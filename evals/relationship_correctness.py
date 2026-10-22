from __future__ import annotations

import logging
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

VALID_CARDINALITIES = {"manyToOne", "oneToMany", "oneToOne", "manyToMany"}


@dataclass
class Relationship:
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    cardinality: str
    cross_filter: str = "singleDirection"
    active: bool = True


@dataclass
class RelEvalResult:
    check: str
    passed: bool
    score: float
    detail: str = ""


def _parse_relationships(tmdl_files: dict[str, str]) -> list[Relationship]:
    rels: list[Relationship] = []
    for content in tmdl_files.values():
        for block in re.finditer(
            r"relationship\s+\w+.*?(?=\nrelationship\s|\npartition\s|\ntable\s|\Z)",
            content,
            re.DOTALL,
        ):
            text = block.group(0)
            from_m = re.search(r"fromColumn\s*[:=]\s*([^\n]+)", text)
            to_m = re.search(r"toColumn\s*[:=]\s*([^\n]+)", text)
            card_m = re.search(r"cardinality\s*[:=]\s*(\w+)", text)
            cross_m = re.search(r"crossFilteringBehavior\s*[:=]\s*(\w+)", text)
            active_m = re.search(r"isActive\s*[:=]\s*(true|false)", text, re.IGNORECASE)
            if not from_m or not to_m:
                continue

            def _split(ref: str) -> tuple[str, str]:
                ref = ref.strip().strip("'")
                if "." in ref:
                    parts = ref.rsplit(".", 1)
                    return parts[0].strip("'"), parts[1]
                return ref, ""

            ft, fc = _split(from_m.group(1))
            tt, tc = _split(to_m.group(1))
            rels.append(
                Relationship(
                    from_table=ft,
                    from_column=fc,
                    to_table=tt,
                    to_column=tc,
                    cardinality=card_m.group(1) if card_m else "manyToOne",
                    cross_filter=cross_m.group(1) if cross_m else "singleDirection",
                    active=(active_m.group(1).lower() != "false") if active_m else True,
                )
            )
    return rels


def _has_cycle(rels: list[Relationship]) -> bool:
    graph: dict[str, set[str]] = defaultdict(set)
    for r in rels:
        if r.active:
            graph[r.from_table].add(r.to_table)

    visited: set[str] = set()

    def _bfs_cycle(start: str) -> bool:
        queue = deque([start])
        seen = {start}
        while queue:
            node = queue.popleft()
            for neighbour in graph.get(node, []):
                if neighbour == start:
                    return True
                if neighbour not in seen:
                    seen.add(neighbour)
                    queue.append(neighbour)
        return False

    for table in list(graph.keys()):
        if table not in visited:
            if _bfs_cycle(table):
                return True
            visited.add(table)
    return False


def _check_date_table(tmdl_files: dict[str, str]) -> bool:
    for content in tmdl_files.values():
        if re.search(r"isDateTable\s*=\s*true", content, re.IGNORECASE):
            return True
    return False


def _has_date_columns(tmdl_files: dict[str, str]) -> bool:
    for content in tmdl_files.values():
        if re.search(r"dataType\s*=\s*dateTime", content, re.IGNORECASE):
            return True
    return False


def run_relationship_evals(
    tmdl_files: dict[str, str],
    langfuse_tracer: Any | None = None,
) -> tuple[list[RelEvalResult], float]:
    rels = _parse_relationships(tmdl_files)
    results: list[RelEvalResult] = []

    table_count = sum(
        len(re.findall(r"^table\s+\S", content, re.MULTILINE))
        for content in tmdl_files.values()
    )
    if table_count > 1:
        results.append(
            RelEvalResult(
                check="relationships_exist",
                passed=bool(rels),
                score=1.0 if rels else 0.0,
                detail=(
                    ""
                    if rels
                    else f"{table_count} tables but NO relationships — the model is "
                    f"disconnected, so no cross-table filtering or measure works"
                ),
            )
        )

    if rels:
        cycle = _has_cycle(rels)
        results.append(
            RelEvalResult(
                check="no_cycles",
                passed=not cycle,
                score=0.0 if cycle else 1.0,
                detail="Circular relationship chain detected" if cycle else "",
            )
        )

    bad_card = [r for r in rels if r.cardinality not in VALID_CARDINALITIES]
    results.append(
        RelEvalResult(
            check="valid_cardinality",
            passed=not bad_card,
            score=0.0 if bad_card else 1.0,
            detail=(
                f"Invalid cardinalities: {[r.cardinality for r in bad_card]}"
                if bad_card
                else ""
            ),
        )
    )

    seen: set[tuple[str, str, str, str]] = set()
    dupes: list[str] = []
    for r in rels:
        key = (r.from_table, r.from_column, r.to_table, r.to_column)
        if key in seen:
            dupes.append(f"{r.from_table}[{r.from_column}]→{r.to_table}[{r.to_column}]")
        seen.add(key)
    results.append(
        RelEvalResult(
            check="no_duplicates",
            passed=not dupes,
            score=0.0 if dupes else 1.0,
            detail=f"Duplicate relationships: {dupes}" if dupes else "",
        )
    )

    if _has_date_columns(tmdl_files):
        has_dt = _check_date_table(tmdl_files)
        results.append(
            RelEvalResult(
                check="date_table_marked",
                passed=has_dt,
                score=1.0 if has_dt else 0.5,
                detail=(
                    ""
                    if has_dt
                    else "Date columns present but no table marked isDateTable"
                ),
            )
        )

    if not results:
        return [], 1.0

    aggregate = round(sum(r.score for r in results) / len(results), 3)

    if langfuse_tracer is not None:
        for r in results:
            langfuse_tracer.score(
                name=f"relationship.{r.check}",
                value=r.score,
                comment=r.detail or "ok",
            )
        langfuse_tracer.score(
            name="relationship_correctness_aggregate",
            value=aggregate,
            comment=f"{len([r for r in results if not r.passed])} check(s) failed",
        )

    failed = [r for r in results if not r.passed]
    if failed:
        logger.warning(
            "relationship_correctness: %d check(s) failed: %s",
            len(failed),
            [(r.check, r.detail) for r in failed],
        )
    return results, aggregate
