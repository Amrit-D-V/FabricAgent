from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

from semantic_model_v2.reference_model_loader import ReferenceModel, load_pbip_reference
from semantic_model_v2.requirements_loader import RequirementsDoc, load_requirements_csv

ROOT = (
    Path(__file__).resolve().parent.parent
)  # repo root (module now lives under model/)
REF_PBIP = ROOT / "EAPS_Backup3.pbip"
GEN_PBIP = ROOT / "models" / "test_2_.pbip"
KPI_CSV = ROOT / "Data Mapping 20250812 V01.1 (final draft).csv"
REPORT = ROOT / "validation_report.md"


_WORD = re.compile(r"[a-z0-9]+")


def _tokens(s: str) -> set[str]:
    return set(_WORD.findall((s or "").lower()))


def _kpi_match_score(kpi_name: str, measure_name: str) -> float:
    a, b = _tokens(kpi_name), _tokens(measure_name)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def best_measure_match(
    kpi_name: str, measures: list, threshold: float = 0.5
) -> tuple[str, float]:
    best_name, best_score = "", 0.0
    for m in measures:
        score = _kpi_match_score(kpi_name, m.name)
        if score > best_score:
            best_name, best_score = m.name, score
    if best_score >= threshold:
        return best_name, best_score
    return "", best_score


def model_summary(label: str, model: ReferenceModel) -> str:
    lines = [f"### {label}"]
    lines.append(f"- **Path:** `{model.source_path}`")
    lines.append(f"- **Tables (business):** {len(model.business_tables)}")
    lines.append(f"- **Measures:** {model.measure_count}")
    lines.append(f"- **Relationships:** {model.relationship_count}")
    if model.detected_patterns:
        lines.append(f"- **Patterns detected:** {', '.join(model.detected_patterns)}")
    by_kind = Counter(t.kind for t in model.business_tables)
    if by_kind:
        kinds = ", ".join(f"{n}×{k}" for k, n in by_kind.most_common())
        lines.append(f"- **Table kinds:** {kinds}")
    return "\n".join(lines)


def table_set_diff(ref: ReferenceModel, gen: ReferenceModel) -> str:
    ref_names = {t.name for t in ref.business_tables}
    gen_names = {t.name for t in gen.business_tables}
    only_ref = sorted(ref_names - gen_names)
    only_gen = sorted(gen_names - ref_names)
    shared = sorted(ref_names & gen_names)

    lines = ["## Table-level comparison"]
    lines.append(f"- Shared: **{len(shared)}**")
    lines.append(f"- Only in reference (EAPS): **{len(only_ref)}**")
    lines.append(f"- Only in generated (test_2_): **{len(only_gen)}**")
    lines.append("")
    if only_ref:
        lines.append("**Missing from generated model (present in reference):**")
        for n in only_ref:
            lines.append(f"- `{n}`")
        lines.append("")
    if only_gen:
        lines.append("**Extra in generated model (not in reference):**")
        for n in only_gen:
            lines.append(f"- `{n}`")
        lines.append("")
    if shared:
        lines.append(f"<details><summary>Shared tables ({len(shared)})</summary>\n")
        for n in shared:
            lines.append(f"- `{n}`")
        lines.append("\n</details>")
    return "\n".join(lines)


def measure_set_diff(ref: ReferenceModel, gen: ReferenceModel) -> str:
    ref_names = {m.name for m in ref.measures}
    gen_names = {m.name for m in gen.measures}
    only_ref = sorted(ref_names - gen_names)
    only_gen = sorted(gen_names - ref_names)
    shared = sorted(ref_names & gen_names)

    lines = ["## Measure-level comparison (exact-name match)"]
    lines.append(f"- Shared: **{len(shared)}**")
    lines.append(f"- Only in reference: **{len(only_ref)}**")
    lines.append(f"- Only in generated: **{len(only_gen)}**")
    lines.append("")
    if only_ref:
        lines.append(
            f"<details><summary>In EAPS but NOT in test_2_ ({len(only_ref)})</summary>\n"
        )
        for n in only_ref:
            lines.append(f"- {n}")
        lines.append("\n</details>")
    if only_gen:
        lines.append(
            f"\n<details><summary>In test_2_ but NOT in EAPS ({len(only_gen)})</summary>\n"
        )
        for n in only_gen:
            lines.append(f"- {n}")
        lines.append("\n</details>")
    return "\n".join(lines)


def kpi_coverage(label: str, model: ReferenceModel, spec: RequirementsDoc) -> str:
    rows = []
    matched = blocked = ambig = unmatched = 0
    BLOCKED_SOURCES = {"d365", "spc"}
    for kpi in spec.kpis:
        if not kpi.has_actionable_content:
            continue
        kpi_id = kpi.id or str(kpi.row_index)
        name = (kpi.name or "").strip()
        source = (kpi.source or "").strip().lower()

        spec_blocked = (
            any(s in source for s in BLOCKED_SOURCES) and "power apps" not in source
        )

        best, score = best_measure_match(name, model.measures, threshold=0.5)
        if best:
            matched += 1
            status = " MATCH"
            note = f"→ `{best}` (score {score:.2f})"
        elif spec_blocked:
            blocked += 1
            status = " BLOCKED"
            note = f"source={kpi.source!r} — not wired to model"
        else:
            best2, score2 = best_measure_match(name, model.measures, threshold=0.0)
            if best2 and score2 >= 0.3:
                ambig += 1
                status = " AMBIGUOUS"
                note = f"closest `{best2}` (score {score2:.2f})"
            else:
                unmatched += 1
                status = " MISSING"
                note = f"source={kpi.source!r}"
        rows.append((kpi_id, kpi.category or "", name, status, note))

    total = matched + blocked + ambig + unmatched
    lines = [f"## KPI coverage — {label}"]
    lines.append(
        f"-  MATCH: **{matched}** / {total}  " f"({matched / max(total, 1):.0%})"
    )
    lines.append(f"-  AMBIGUOUS (weak match): **{ambig}**")
    lines.append(f"-  BLOCKED (missing source per spec): **{blocked}**")
    lines.append(f"-  MISSING: **{unmatched}**")
    lines.append("")
    lines.append("| # | Category | KPI | Status | Note |")
    lines.append("|---|---|---|---|---|")
    for kid, cat, name, status, note in rows:
        safe = name.replace("|", "\\|")
        safe_note = note.replace("|", "\\|")
        lines.append(f"| {kid} | {cat} | {safe} | {status} | {safe_note} |")
    return "\n".join(lines)


def relationships_block(ref: ReferenceModel, gen: ReferenceModel) -> str:
    lines = ["## Relationships"]
    lines.append(f"- Reference (EAPS): {len(ref.relationships)} relationships")
    lines.append(f"- Generated (test_2_): {len(gen.relationships)} relationships")
    lines.append("")
    lines.append("**Generated model relationships:**")
    if not gen.relationships:
        lines.append("- _(none — fact tables likely won't filter properly)_")
    else:
        for r in gen.relationships:
            arrow = "→" if r.is_active else "⤳ (inactive)"
            lines.append(
                f"- `{r.from_table}[{r.from_column}]` {arrow} "
                f"`{r.to_table}[{r.to_column}]`"
            )
    return "\n".join(lines)


def main() -> int:
    if not GEN_PBIP.exists():
        print(f"ERROR: generated PBIP not found at {GEN_PBIP}", file=sys.stderr)
        return 1
    if not KPI_CSV.exists():
        print(f"ERROR: KPI spec not found at {KPI_CSV}", file=sys.stderr)
        return 1

    ref = None
    if REF_PBIP.exists():
        print(f"Loading reference: {REF_PBIP.name} ...")
        ref = load_pbip_reference(REF_PBIP)
        print(f"  ->{len(ref.business_tables)} tables, {ref.measure_count} measures")
    else:
        print(
            f"(no reference PBIP at {REF_PBIP.name} — skipping side-by-side sections)"
        )

    print(f"Loading generated: {GEN_PBIP.name} ...")
    gen = load_pbip_reference(GEN_PBIP)
    print(f"  ->{len(gen.business_tables)} tables, {gen.measure_count} measures")

    print(f"Loading KPI spec: {KPI_CSV.name} ...")
    spec = load_requirements_csv(KPI_CSV)
    actionable = [k for k in spec.kpis if k.has_actionable_content]
    print(f"  ->{len(actionable)} actionable KPI rows")

    sections: list[str] = [
        "# Semantic Model Validation Report",
        "",
        (
            "Compares the **generated** semantic model against the **reference** "
            "(expert-built) model and the **client KPI spec**."
            if ref is not None
            else "Reports the **generated** semantic model against the **client KPI spec** "
            "(no reference model attached)."
        ),
        "",
        "## Model summaries",
        "",
    ]
    if ref is not None:
        sections += [model_summary("Reference", ref), ""]
    sections += [model_summary("Generated — test_2_", gen), ""]
    if ref is not None:
        sections += [
            table_set_diff(ref, gen),
            "",
            relationships_block(ref, gen),
            "",
            measure_set_diff(ref, gen),
            "",
        ]
    sections += [
        kpi_coverage("Generated (test_2_) vs spec", gen, spec),
        "",
    ]
    if ref is not None:
        sections += [
            "---",
            "",
            kpi_coverage("Reference vs spec", ref, spec),
            "",
        ]

    REPORT.write_text("\n".join(sections), encoding="utf-8")
    print(f"\nReport written: {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
