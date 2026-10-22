from __future__ import annotations

import sys

try:
    _reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(_reconfigure):
        _reconfigure(encoding="utf-8")
except Exception:
    pass
from collections import defaultdict, deque
from collections.abc import Iterable
from pathlib import Path

from semantic_model_v2 import (
    ColumnSpec,
    MeasureSpec,
    RelationshipSpec,
    SchemaPlan,
    TableSpec,
    load_pbip_reference,
    validate_measures_business,
)


def _supports_color() -> bool:
    return sys.stdout.isatty() and not sys.platform.startswith("win")


_USE_COLOR = _supports_color()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def red(s: str) -> str:
    return _c("31", s)


def yellow(s: str) -> str:
    return _c("33", s)


def green(s: str) -> str:
    return _c("32", s)


def dim(s: str) -> str:
    return _c("2", s)


def find_ambiguous_paths(
    rels: list[tuple[str, str, bool]],
) -> list[tuple[str, str, str, str]]:
    rel_graph: dict[str, set[str]] = defaultdict(set)
    for from_t, to_t, active in rels:
        if active:
            rel_graph[to_t].add(from_t)

    def reachable(start: str) -> set[str]:
        seen: set[str] = set()
        q = deque([start])
        while q:
            n = q.popleft()
            if n in seen:
                continue
            seen.add(n)
            q.extend(rel_graph.get(n, ()))
        seen.discard(start)
        return seen

    found: list[tuple[str, str, str, str]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for parent, kids in rel_graph.items():
        if len(kids) < 2:
            continue
        kid_desc = {c: (reachable(c) | {c}) for c in kids}
        sorted_kids = sorted(kids)
        for i in range(len(sorted_kids)):
            for j in range(i + 1, len(sorted_kids)):
                ci, cj = sorted_kids[i], sorted_kids[j]
                overlap = kid_desc[ci] & kid_desc[cj]
                for target in overlap:
                    if (parent, target) in seen_pairs:
                        continue
                    seen_pairs.add((parent, target))
                    found.append((parent, target, ci, cj))
    return found


def find_orphan_tables(
    table_names: Iterable[str],
    rels: list[tuple[str, str, bool]],
) -> list[str]:
    referenced: set[str] = set()
    for from_t, to_t, active in rels:
        if active:
            referenced.add(from_t)
            referenced.add(to_t)
    return sorted(
        t
        for t in table_names
        if t not in referenced
        and not t.startswith("_")
        and t.lower() not in {"dimdate", "datetable"}
    )


def _ref_to_plan(ref) -> SchemaPlan:
    plan = SchemaPlan(
        model_name=ref.model_name,
        server_fqdn="",
        db_name="",
    )
    for t in ref.business_tables:
        plan.tables.append(
            TableSpec(
                name=t.name,
                kind=t.kind if t.kind != "unknown" else "dim",
                columns=[
                    ColumnSpec(
                        name=c.name,
                        source_column=c.name,
                        data_type=c.data_type or "string",
                        is_hidden=c.is_hidden,
                        is_key=c.is_key,
                    )
                    for c in t.columns
                ],
            )
        )
    for m in ref.measures:
        plan.measures.append(
            MeasureSpec(
                name=m.name,
                host_table=m.host_table,
                intent="",
                template_id=None,
                format_string=m.format_string,
                display_folder=m.display_folder,
                dax=m.dax or "",
            )
        )
    for r in ref.relationships:
        plan.relationships.append(
            RelationshipSpec(
                from_table=r.from_table,
                from_column=r.from_column,
                to_table=r.to_table,
                to_column=r.to_column,
                is_active=r.is_active,
                cross_filter=r.cross_filter,
            )
        )
    return plan


def lint(pbip_path: str | Path) -> int:
    try:
        ref = load_pbip_reference(pbip_path)
    except Exception as e:
        print(red(f"Could not load PBIP: {e}"))
        return 2

    biz_tables = ref.business_tables
    print(dim(f"Loaded: {ref.model_name}"))
    print(
        dim(
            f"  {len(biz_tables)} business tables · "
            f"{ref.measure_count} measures · "
            f"{ref.relationship_count} relationships"
        )
    )
    if ref.detected_patterns:
        print(dim(f"  Patterns: {len(ref.detected_patterns)}"))
    print()

    error_count = 0
    warning_count = 0
    info_count = 0

    rel_tuples = [(r.from_table, r.to_table, r.is_active) for r in ref.relationships]
    ambig = find_ambiguous_paths(rel_tuples)
    if ambig:
        for parent, target, c1, c2 in ambig:
            print(
                red(
                    f"[ERROR] Ambiguous filter path: '{parent}' -> '{target}' "
                    f"reachable via both '{c1}' and '{c2}'."
                )
            )
            print(
                dim(
                    "        Power BI Desktop will reject the model. Inactivate "
                    "one of the conflicting edges."
                )
            )
            error_count += 1
    else:
        print(green("[OK]    No ambiguous filter paths."))

    orphans = find_orphan_tables([t.name for t in biz_tables], rel_tuples)
    if orphans:
        for o in orphans:
            print(
                yellow(
                    f"[WARN]  Orphan table '{o}' — no active relationships. "
                    f"Won't be slice-able from any visual."
                )
            )
            warning_count += 1

    inactives = [r for r in ref.relationships if not r.is_active]
    if inactives:
        print(dim(f"[INFO]  {len(inactives)} inactive relationship(s):"))
        for r in inactives:
            print(
                dim(
                    f"          {r.from_table}.{r.from_column} -> {r.to_table}.{r.to_column}"
                )
            )
        info_count += len(inactives)

    plan = _ref_to_plan(ref)
    biz_issues = validate_measures_business(plan, profile=None)
    biz_errs = [i for i in biz_issues if i.severity == "error"]
    biz_warns = [i for i in biz_issues if i.severity == "warning"]
    biz_info = [i for i in biz_issues if i.severity == "info"]
    if biz_errs:
        for i in biz_errs:
            print(red(f"[ERROR] measure '{i.measure}' ({i.category}): {i.message}"))
        error_count += len(biz_errs)
    if biz_warns:
        for i in biz_warns:
            print(yellow(f"[WARN]  measure '{i.measure}' ({i.category}): {i.message}"))
        warning_count += len(biz_warns)
    if biz_info:
        for i in biz_info:
            print(dim(f"[INFO]  measure '{i.measure}' ({i.category}): {i.message}"))
        info_count += len(biz_info)
    if not biz_issues:
        print(green("[OK]    No measure DAX issues."))

    print()
    summary = " · ".join(
        s
        for s in [
            red(f"{error_count} error(s)") if error_count else "",
            yellow(f"{warning_count} warning(s)") if warning_count else "",
            dim(f"{info_count} info") if info_count else "",
        ]
        if s
    )
    if not summary:
        print(green("CLEAN — Power BI Desktop should load this PBIP without errors."))
    else:
        print(summary)
        if error_count:
            print(red("Errors will block Power BI Desktop from loading this file."))

    return 1 if error_count else 0


def _main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python lint_pbip.py <path-to-.pbip-or-folder>")
        return 2
    return lint(sys.argv[1])


if __name__ == "__main__":
    sys.exit(_main())
