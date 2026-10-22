from __future__ import annotations

import re

from .plan import MeasureSpec, SchemaPlan

_KPI_LINE = re.compile(
    r"^-\s*(?:\[\d+\]\s+)?([^·]+?)\s*·\s+([^()\n—]+?)\s*(?:\(([^)]+)\)|—|$)"
)
_TOK = re.compile(r"[a-z0-9]+")


def _tokens(s: str) -> set[str]:
    return set(_TOK.findall((s or "").lower()))


def _format_for_unit(unit: str) -> str | None:
    u = (unit or "").strip().lower()
    if not u:
        return None
    if "%" in u or "percent" in u or "ratio" in u:
        return "0.0%"
    if u in {"count", "#", "qty", "nos", "number", "n", "no"}:
        return "#,##0"
    if u in {"min", "mins", "minutes", "hr", "hrs", "hours", "sec", "seconds"}:
        return "#,##0"
    if "currency" in u or "$" in u or u in {"usd", "eur", "inr"}:
        return "#,##0.00"
    return None


def _pick_host_table(plan: SchemaPlan, category: str, source: str) -> str | None:
    facts = [t for t in plan.tables if t.kind == "fact"]
    if not facts:
        return None

    hint = f"{category} {source}".lower()
    if not hint.strip():
        return facts[0].name

    best, best_score = facts[0].name, -1
    for t in facts:
        st = (t.source_table or t.name or "").lower()
        words = [w for w in re.split(r"[._\s]+", st) if len(w) > 3]
        score = sum(1 for w in words if w in hint)
        if score > best_score:
            best_score = score
            best = t.name
    return best


def _extract_extras(line: str) -> tuple[str, str, str]:
    if "—" not in line:
        return "", "", ""
    tail = line.split("—", 1)[1]
    parts = [p.strip() for p in tail.split("|")]
    formula = src = levels = ""
    for p in parts:
        low = p.lower()
        if low.startswith("formula:"):
            formula = p.split(":", 1)[1].strip()
        elif low.startswith("src:"):
            src = p.split(":", 1)[1].strip()
        elif low.startswith("levels:"):
            levels = p.split(":", 1)[1].strip()
    return formula, src, levels


def _already_covered(
    full_name: str,
    existing: list[tuple[set[str], str]],
    threshold: float = 0.5,
) -> bool:
    a = _tokens(full_name)
    if not a:
        return True
    for b, _ in existing:
        if not b:
            continue
        j = len(a & b) / len(a | b)
        if j >= threshold:
            return True
    return False


def seed_measures_from_kpi_block(
    plan: SchemaPlan,
    kpi_block: str,
) -> tuple[list[MeasureSpec], list[str], list[str]]:
    new_specs: list[MeasureSpec] = []
    already: list[str] = []
    no_host: list[str] = []
    seen: set[str] = set()

    existing = [(_tokens(m.name), m.name) for m in plan.measures if m.name]

    for raw_line in kpi_block.splitlines():
        line = raw_line.strip()
        m = _KPI_LINE.match(line)
        if not m:
            continue
        category = m.group(1).strip()
        kpi_name = m.group(2).strip()
        unit = (m.group(3) or "").strip() if m.group(3) else ""
        full_name = f"{category} {kpi_name}".strip()
        key = full_name.lower()
        if key in seen:
            continue
        seen.add(key)

        if _already_covered(full_name, existing):
            already.append(full_name)
            continue

        formula, source, levels = _extract_extras(line)
        host = _pick_host_table(plan, category, source)
        if not host:
            no_host.append(full_name)
            continue

        intent_parts = [f"KPI {full_name}"]
        if formula:
            intent_parts.append(f"formula: {formula}")
        if source:
            intent_parts.append(f"src: {source}")
        if levels:
            intent_parts.append(f"levels: {levels}")
        intent = " · ".join(intent_parts)

        spec = MeasureSpec(
            name=full_name,
            host_table=host,
            intent=intent,
            template_id=None,
            format_string=_format_for_unit(unit),
            display_folder=f"KPIs/{category}",
        )
        new_specs.append(spec)
        existing.append((_tokens(full_name), full_name))

    return new_specs, already, no_host
