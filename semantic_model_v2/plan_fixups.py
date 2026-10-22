from __future__ import annotations

import re
from datetime import datetime

from config import (
    SMB_COUNT_FN_RX,
    SMB_DATE_NAME_RX,
    SMB_DATE_PARSE_FORMATS,
    SMB_FMT_DECIMAL,
    SMB_FMT_PERCENT,
    SMB_FMT_WHOLE,
    SMB_INT_NAME_RX,
    SMB_PCT_NAME_RX,
    SMB_POSTAL_NAME_RX,
    SMB_TREND_BASE_GROUPS,
    SMB_TREND_FOLDER,
    SMB_TREND_MAX_BASES,
    SMB_TREND_PCT_FORMAT,
)

from .plan import ColumnSpec, MeasureSpec, SchemaPlan, TableSpec


def _date_dim_keys(plan: SchemaPlan) -> dict[str, str]:
    out: dict[str, str] = {}
    for t in plan.tables:
        if t.kind != "calculated":
            continue
        for c in t.columns:
            if c.is_key and c.data_type == "dateTime":
                out[t.name] = c.name
                break
    return out


def _find_month_number_col(t: TableSpec) -> str | None:
    for c in t.columns:
        if c.name.lower() == "month":
            return c.name
    for c in t.columns:
        expr = (c.dax_expression or "").replace(" ", "").upper()
        if expr.startswith("MONTH("):
            return c.name
    return None


def _is_month_name_col(c: ColumnSpec) -> bool:
    if c.name.lower().replace(" ", "") in (
        "monthname",
        "monthnameshort",
        "monthnamelong",
    ):
        return True
    expr = (c.dax_expression or "").upper().replace(" ", "")
    return "FORMAT(" in expr and ('"MMM"' in expr or '"MMMM"' in expr)


def fix_timestamp_date_keys(plan: SchemaPlan) -> list[str]:
    actions: list[str] = []
    date_dims = _date_dim_keys(plan)
    if not date_dims:
        return actions
    by_name = {t.name: t for t in plan.tables}
    for r in plan.relationships:
        if date_dims.get(r.to_table) != r.to_column:
            continue
        from_t = by_name.get(r.from_table)
        if from_t is None or from_t.kind == "calculated":
            continue
        src_col = next((c for c in from_t.columns if c.name == r.from_column), None)
        if src_col is None or src_col.data_type != "dateTime":
            continue
        if src_col.dax_expression:
            continue
        key_name = f"{r.from_column} Date Key"
        if not any(c.name == key_name for c in from_t.columns):
            from_t.columns.append(
                ColumnSpec(
                    name=key_name,
                    source_column=key_name,
                    data_type="dateTime",
                    is_hidden=True,
                    summarize_by="none",
                    dax_expression=(
                        f"DATE(YEAR([{r.from_column}]), "
                        f"MONTH([{r.from_column}]), DAY([{r.from_column}]))"
                    ),
                )
            )
            actions.append(
                f"injected hidden date key '{from_t.name}'[{key_name}] "
                f"(time-stripped {r.from_column}) and re-pointed its Date relationship"
            )
        r.from_column = key_name
    return actions


def set_date_dim_sort_columns(plan: SchemaPlan) -> list[str]:
    actions: list[str] = []
    for t in plan.tables:
        if t.kind != "calculated":
            continue
        if not any(c.is_key and c.data_type == "dateTime" for c in t.columns):
            continue
        month_num = _find_month_number_col(t)
        if not month_num:
            continue
        for c in t.columns:
            if c.sort_by or c.name == month_num:
                continue
            if _is_month_name_col(c):
                c.sort_by = month_num
                actions.append(f"set '{t.name}'[{c.name}] to sort by [{month_num}]")
    return actions


def hide_fact_relationship_keys(plan: SchemaPlan) -> list[str]:
    actions: list[str] = []
    fact_names = {t.name for t in plan.tables if t.kind == "fact"}
    by_name = {t.name: t for t in plan.tables}
    for r in plan.relationships:
        if r.from_table not in fact_names:
            continue
        t = by_name.get(r.from_table)
        if t is None:
            continue
        col = next((c for c in t.columns if c.name == r.from_column), None)
        if col is None or col.data_type != "string" or col.is_hidden:
            continue
        col.is_hidden = True
        actions.append(f"hid fact FK key '{t.name}'[{col.name}]")
    return actions


_POSTAL_RX = re.compile(SMB_POSTAL_NAME_RX, re.IGNORECASE)


def coerce_postal_codes_to_text(plan: SchemaPlan) -> list[str]:
    actions: list[str] = []
    for t in plan.tables:
        if t.kind == "calculated":
            continue
        for c in t.columns:
            if c.data_type not in ("int64", "double") or c.dax_expression:
                continue
            if not _POSTAL_RX.search(c.name):
                continue
            c.data_type = "string"
            c.m_type_cast = "type text"
            actions.append(
                f"coerced '{t.name}'[{c.name}] to text (postal code; keep leading zeros)"
            )
    return actions


_DATE_NAME_RX = re.compile(SMB_DATE_NAME_RX, re.IGNORECASE)
_INT_NAME_RX = re.compile(SMB_INT_NAME_RX, re.IGNORECASE)
_DATE_FORMATS = tuple(SMB_DATE_PARSE_FORMATS)


def _nonempty(samples) -> list:
    return [s for s in samples if s is not None and str(s).strip() != ""]


def _try_strptime(txt: str, fmt: str) -> bool:
    try:
        datetime.strptime(txt, fmt)
        return True
    except ValueError:
        return False


def _all_parse_int(samples) -> bool:
    vals = _nonempty(samples)
    if not vals:
        return False
    for s in vals:
        try:
            int(str(s).strip())
        except (ValueError, TypeError):
            return False
    return True


def _all_parse_date(samples) -> bool:
    vals = _nonempty(samples)
    if not vals:
        return False
    for s in vals:
        txt = str(s).strip()
        try:
            datetime.fromisoformat(txt)
            continue
        except ValueError:
            pass
        if not any(_try_strptime(txt, f) for f in _DATE_FORMATS):
            return False
    return True


def coerce_stringly_typed_columns(plan: SchemaPlan, profile: dict | None) -> list[str]:
    actions: list[str] = []
    if not profile:
        return actions
    for t in plan.tables:
        if t.kind == "calculated":
            continue
        cols = (profile.get(t.name) or {}).get("columns") or {}
        for c in t.columns:
            if c.data_type != "string" or c.dax_expression or c.m_value_coerce:
                continue
            samples = (cols.get(c.source_column) or cols.get(c.name) or {}).get(
                "samples"
            ) or []
            if not samples:
                continue
            if _DATE_NAME_RX.search(c.name) and _all_parse_date(samples):
                c.data_type, c.m_value_coerce = "dateTime", "dateTime"
                actions.append(f"coerced '{t.name}'[{c.name}] to dateTime (text dates)")
            elif _INT_NAME_RX.search(c.name) and _all_parse_int(samples):
                c.data_type, c.m_value_coerce = "int64", "int64"
                actions.append(f"coerced '{t.name}'[{c.name}] to int64 (text numbers)")
    return actions


def _norm_dax(dax: str) -> str:
    return "".join((dax or "").split())


def dedup_auto_measures(plan: SchemaPlan) -> list[str]:
    actions: list[str] = []
    curated = {_norm_dax(m.dax) for m in plan.measures if not m.auto_injected and m.dax}
    kept = []
    for m in plan.measures:
        if m.auto_injected and m.dax and _norm_dax(m.dax) in curated:
            actions.append(
                f"dropped duplicate auto measure '{m.name}' (matches a curated measure)"
            )
        else:
            kept.append(m)
    plan.measures = kept
    return actions


_FOLDER_TREND = SMB_TREND_FOLDER
_MAX_TREND_BASES = SMB_TREND_MAX_BASES
_NON_BASE_RX = re.compile(r"DIVIDE\(|SAMEPERIODLASTYEAR|DATEADD", re.IGNORECASE)
_TREND_GROUPS = tuple(re.compile(g, re.IGNORECASE) for g in SMB_TREND_BASE_GROUPS)


def _active_date_ref(plan: SchemaPlan) -> str | None:
    date_dims = _date_dim_keys(plan)
    for r in plan.relationships:
        if r.is_active and date_dims.get(r.to_table) == r.to_column:
            return f"'{r.to_table}'[{r.to_column}]"
    return None


def _pick_trend_bases(plan: SchemaPlan) -> list[MeasureSpec]:
    bases: list[MeasureSpec] = []
    seen: set[str] = set()
    for group in _TREND_GROUPS:
        for m in plan.measures:
            if m.auto_injected or not m.dax or _NON_BASE_RX.search(m.dax):
                continue
            if m.name in seen:
                continue
            if group.search(m.name):
                bases.append(m)
                seen.add(m.name)
                break
    return bases[:_MAX_TREND_BASES]


def inject_time_intelligence(plan: SchemaPlan) -> list[str]:
    actions: list[str] = []
    date_ref = _active_date_ref(plan)
    if not date_ref:
        return actions
    existing = {m.name for m in plan.measures}
    for b in _pick_trend_bases(plan):
        py = f"CALCULATE([{b.name}], SAMEPERIODLASTYEAR({date_ref}))"
        mom_prev = f"CALCULATE([{b.name}], DATEADD({date_ref}, -1, MONTH))"
        for name, dax, fmt in (
            (f"{b.name} PY", py, b.format_string),
            (
                f"{b.name} YoY %",
                f"DIVIDE([{b.name}] - {py}, {py})",
                SMB_TREND_PCT_FORMAT,
            ),
            (
                f"{b.name} MoM %",
                f"DIVIDE([{b.name}] - {mom_prev}, {mom_prev})",
                SMB_TREND_PCT_FORMAT,
            ),
        ):
            if name in existing:
                continue
            plan.measures.append(
                MeasureSpec(
                    name=name,
                    host_table=b.host_table,
                    intent=f"Time-intelligence trend for {b.name}",
                    format_string=fmt,
                    display_folder=_FOLDER_TREND,
                    dax=dax,
                    auto_injected=True,
                )
            )
            existing.add(name)
            actions.append(f"injected trend measure '{name}'")
    return actions


def apply_plan_fixups(plan: SchemaPlan, profile: dict | None = None) -> list[str]:
    actions: list[str] = []
    actions += fix_timestamp_date_keys(plan)
    actions += set_date_dim_sort_columns(plan)
    actions += hide_fact_relationship_keys(plan)
    actions += coerce_postal_codes_to_text(plan)
    actions += coerce_stringly_typed_columns(plan, profile)
    return actions


def fix_percentage_dax_scaling(plan: SchemaPlan) -> list[str]:
    actions: list[str] = []
    for m in plan.measures:
        dax = m.dax or ""
        fmt = m.format_string or ""
        # If measure has % format string and DAX multiplies by 100, remove the double multiplier
        if "%" in fmt and re.search(r"\s*\*\s*100(?:\.0+)?\b", dax):
            new_dax = re.sub(r"\s*\*\s*100(?:\.0+)?\b", "", dax).strip()
            m.dax = new_dax
            actions.append(
                f"removed redundant '* 100' from percentage measure [{m.name}] (formatString handles % scaling)"
            )
    return actions


def apply_post_dax_fixups(plan: SchemaPlan) -> list[str]:
    actions: list[str] = []
    actions += inject_time_intelligence(plan)
    actions += dedup_auto_measures(plan)
    actions += infer_missing_format_strings(plan)
    actions += fix_percentage_dax_scaling(plan)
    actions += flag_dax_perf_smells(plan)
    return actions


_PCT_NAME_RX = re.compile(SMB_PCT_NAME_RX, re.IGNORECASE)
_COUNT_FN_RX = re.compile(SMB_COUNT_FN_RX, re.IGNORECASE)
_DIVIDE_RX = re.compile(r"\bDIVIDE\s*\(|\)\s*/\s*[\[(]|\]\s*/\s*[\[(]", re.IGNORECASE)


def _infer_format(m, plan: SchemaPlan) -> str | None:
    from model.dax_analyze import _STRIP  # strip strings/comments before scanning
    from model.dax_preflight import extract_references

    dax = _STRIP.sub(" ", m.dax or "")
    if not dax.strip():
        return None
    # Any measure with % / percent / rate / ratio / margin / share in name or intent should format as %
    if _PCT_NAME_RX.search(m.name or "") or _PCT_NAME_RX.search(
        getattr(m, "intent", "") or ""
    ):
        return SMB_FMT_PERCENT
    if _DIVIDE_RX.search(dax):
        return SMB_FMT_PERCENT
    if _COUNT_FN_RX.search(dax) and "DIVIDE" not in dax.upper():
        return SMB_FMT_WHOLE
    if "DIVIDE" not in dax.upper():
        cols, _meas = extract_references(dax)
        types = {
            c.data_type
            for t in plan.tables
            for c in t.columns
            for (rt, rc) in cols
            if t.name == rt and c.name == rc
        }
        if types == {"int64"}:
            return SMB_FMT_WHOLE
        if types and types <= {"int64", "double", "decimal"}:
            return SMB_FMT_DECIMAL
    return None


def infer_missing_format_strings(plan: SchemaPlan) -> list[str]:
    actions: list[str] = []
    for m in plan.measures:
        # If format_string is missing or if a percentage-named metric was mistakenly given a non-percentage format
        if not m.format_string:
            fmt = _infer_format(m, plan)
            if fmt:
                m.format_string = fmt
                actions.append(
                    f"inferred format '{fmt}' for [{m.name}] (LLM left it blank)"
                )
        elif _PCT_NAME_RX.search(m.name or "") and "%" not in (m.format_string or ""):
            # Overwrite non-percentage format on percentage metric
            m.format_string = SMB_FMT_PERCENT
            actions.append(
                f"corrected format to '{SMB_FMT_PERCENT}' for percentage measure [{m.name}] (was '{m.format_string}')"
            )
    return actions


def flag_dax_perf_smells(plan: SchemaPlan) -> list[str]:
    from model.dax_analyze import dax_smells

    actions: list[str] = []
    for m in plan.measures:
        for issue, _fix in dax_smells(m.dax or ""):
            actions.append(f"perf note — [{m.name}]: {issue}")
    return actions
