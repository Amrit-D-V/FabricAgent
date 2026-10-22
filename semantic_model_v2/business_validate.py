from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal

from model.dax_preflight import extract_references

from .plan import SchemaPlan

logger = logging.getLogger(__name__)


_DAX_TIME_INTEL_FUNCS = {
    "TOTALYTD",
    "TOTALMTD",
    "TOTALQTD",
    "SAMEPERIODLASTYEAR",
    "PARALLELPERIOD",
    "DATEADD",
    "DATESBETWEEN",
    "DATESINPERIOD",
    "DATESYTD",
    "DATESMTD",
    "DATESQTD",
    "PREVIOUSYEAR",
    "PREVIOUSQUARTER",
    "PREVIOUSMONTH",
    "PREVIOUSDAY",
    "NEXTYEAR",
    "NEXTQUARTER",
    "NEXTMONTH",
    "NEXTDAY",
    "OPENINGBALANCEYEAR",
    "OPENINGBALANCEMONTH",
    "OPENINGBALANCEQUARTER",
    "CLOSINGBALANCEYEAR",
    "CLOSINGBALANCEMONTH",
    "CLOSINGBALANCEQUARTER",
    "STARTOFYEAR",
    "STARTOFQUARTER",
    "STARTOFMONTH",
    "ENDOFYEAR",
    "ENDOFQUARTER",
    "ENDOFMONTH",
    "FIRSTDATE",
    "LASTDATE",
}


@dataclass
class BusinessIssue:

    severity: Literal["error", "warning", "info"]
    measure: str
    category: str
    message: str

    def __str__(self) -> str:
        return f"[{self.severity.upper()}] {self.measure} ({self.category}): {self.message}"


_RX_DIVIDE_CALL = re.compile(r"\bDIVIDE\s*\(", re.IGNORECASE)
_RX_STRING_LITERAL = re.compile(r'"([^"]*)"')


def _is_balanced(dax: str) -> tuple[bool, str]:
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[str] = []
    in_string = False
    i = 0
    while i < len(dax):
        ch = dax[i]
        if ch == '"':
            if in_string and i + 1 < len(dax) and dax[i + 1] == '"':
                i += 2
                continue
            in_string = not in_string
            i += 1
            continue
        if in_string:
            i += 1
            continue
        if ch in "([{":
            stack.append(ch)
        elif ch in ")]}":
            if not stack:
                return False, f"unexpected '{ch}' at pos {i}"
            top = stack.pop()
            if top != pairs[ch]:
                return False, f"mismatched '{top}' closed by '{ch}' at pos {i}"
        i += 1
    if stack:
        return False, f"unclosed '{stack[-1]}' (depth={len(stack)})"
    if in_string:
        return False, "unterminated string literal"
    return True, ""


def _find_divide_missing_guard(dax: str) -> list[int]:
    issues: list[int] = []
    for m in _RX_DIVIDE_CALL.finditer(dax):
        start = m.end()
        depth = 1
        comma_count = 0
        i = start
        in_string = False
        while i < len(dax) and depth > 0:
            ch = dax[i]
            if ch == '"' and not (in_string and i + 1 < len(dax) and dax[i + 1] == '"'):
                in_string = not in_string
            elif not in_string:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                elif ch == "," and depth == 1:
                    comma_count += 1
            i += 1
        if comma_count == 1:
            issues.append(m.start())
    return issues


def _column_refs(dax: str) -> set[tuple[str, str]]:
    columns, _ = extract_references(dax)
    return set(columns)


def _measure_refs(dax: str) -> set[str]:
    _, measures = extract_references(dax)
    return set(measures)


def _string_literals(dax: str) -> list[str]:
    return [m.group(1) for m in _RX_STRING_LITERAL.finditer(dax)]


def _has_date_dim(plan: SchemaPlan) -> bool:
    for t in plan.tables:
        low = t.name.lower()
        if "date" in low or low.endswith("dim") and "date" in low:
            return True
        if t.kind == "calculated":
            for c in t.columns:
                if (c.data_type or "").lower() == "datetime" and c.is_key:
                    return True
    return False


def _build_table_col_index(plan: SchemaPlan) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for t in plan.tables:
        out[t.name] = {c.name for c in t.columns}
    return out


def _build_profile_value_index(
    profile: dict | None,
) -> dict[tuple[str, str], set[str]]:
    if not profile:
        return {}
    out: dict[tuple[str, str], set[str]] = {}
    for tname, tinfo in profile.items():
        for col_name, col_info in (tinfo.get("columns") or {}).items():
            samples = col_info.get("samples") or col_info.get("sample_values") or []
            if samples:
                out[(tname, col_name)] = {str(s) for s in samples if s is not None}
    return out


def validate_measures_business(
    plan: SchemaPlan,
    profile: dict | None = None,
) -> list[BusinessIssue]:
    issues: list[BusinessIssue] = []
    table_cols = _build_table_col_index(plan)
    profile_values = _build_profile_value_index(profile)
    has_date_dim = _has_date_dim(plan)
    measure_names = {m.name for m in plan.measures}
    plan_table_names = {t.name for t in plan.tables}
    all_column_names: set[str] = set()
    for _t in plan.tables:
        for _c in _t.columns:
            all_column_names.add(_c.name)

    for measure in plan.measures:
        dax = (measure.dax or "").strip()
        if not dax:
            continue
        if dax == "BLANK()":
            issues.append(
                BusinessIssue(
                    severity="error",
                    measure=measure.name,
                    category="placeholder",
                    message="DAX generation failed — shipped as BLANK() placeholder; returns no value, fix before deploy",
                )
            )
            continue

        ok, why = _is_balanced(dax)
        if not ok:
            issues.append(
                BusinessIssue(
                    severity="error",
                    measure=measure.name,
                    category="syntax",
                    message=f"unbalanced DAX: {why}",
                )
            )
            continue

        for ref in _measure_refs(dax):
            if ref == measure.name:
                issues.append(
                    BusinessIssue(
                        severity="error",
                        measure=measure.name,
                        category="self_ref",
                        message="measure references itself — infinite recursion",
                    )
                )
                break

        for tname, cname in _column_refs(dax):
            if tname not in plan_table_names:
                issues.append(
                    BusinessIssue(
                        severity="error",
                        measure=measure.name,
                        category="column_ref",
                        message=f"references unknown table '{tname}'",
                    )
                )
                continue
            if cname not in table_cols.get(tname, set()) and cname not in measure_names:
                issues.append(
                    BusinessIssue(
                        severity="error",
                        measure=measure.name,
                        category="column_ref",
                        message=f"references unknown column '{tname}'[{cname}]",
                    )
                )

        bare_refs = _measure_refs(dax)
        for ref in bare_refs:
            if ref in measure_names:
                continue
            if ref in all_column_names:
                continue
            issues.append(
                BusinessIssue(
                    severity="warning",
                    measure=measure.name,
                    category="measure_ref",
                    message=f"references unknown measure '[{ref}]'",
                )
            )

        for pos in _find_divide_missing_guard(dax):
            issues.append(
                BusinessIssue(
                    severity="warning",
                    measure=measure.name,
                    category="divide_guard",
                    message=f"DIVIDE( … , … ) at pos {pos} has no 3rd arg — returns BLANK on zero",
                )
            )

        upper = dax.upper()
        fmt = measure.format_string or ""

        # Percentage scaling and format checks
        if "%" in fmt and re.search(r"\*\s*100(?:\.0+)?\b", dax):
            issues.append(
                BusinessIssue(
                    severity="error",
                    measure=measure.name,
                    category="percentage_scaling",
                    message="DAX multiplies by 100 while format_string is percentage — Power BI will display 10,000x the value. Remove '* 100' or use 0.0-1.0 scale.",
                )
            )

        if re.search(
            r"%|percent|\bpct\b|\brate\b|ratio|margin|share|penetration",
            measure.name,
            re.IGNORECASE,
        ):
            if "%" not in fmt:
                issues.append(
                    BusinessIssue(
                        severity="warning",
                        measure=measure.name,
                        category="format_string",
                        message=f"measure name suggests percentage/ratio but format_string is '{fmt or 'null'}' instead of '0.0%'",
                    )
                )
            if re.search(r"\bSUM\s*\(", upper) and not re.search(
                r"\bDIVIDE\s*\(", upper
            ):
                issues.append(
                    BusinessIssue(
                        severity="warning",
                        measure=measure.name,
                        category="aggregation",
                        message="percentage measure uses SUM() directly without DIVIDE() or AVERAGE() — this will sum percentages across rows to thousands of percent",
                    )
                )

        for fn in _DAX_TIME_INTEL_FUNCS:
            if re.search(rf"\b{fn}\s*\(", upper) and not has_date_dim:
                issues.append(
                    BusinessIssue(
                        severity="error",
                        measure=measure.name,
                        category="time_intel",
                        message=f"uses {fn}() but model has no date dim",
                    )
                )

        if profile_values:
            for literal in _string_literals(dax):
                host_keys = [
                    k for k in profile_values if k[0] == measure.host_table
                ]
                if not host_keys:
                    continue
                if literal in {"", "0", "1", "TRUE", "FALSE"}:
                    continue
                if any(literal in profile_values[k] for k in host_keys):
                    continue
                issues.append(
                    BusinessIssue(
                        severity="info",
                        measure=measure.name,
                        category="literal",
                        message=(
                            f'string literal "{literal}" doesn\'t match any '
                            f"sample value on host table '{measure.host_table}'. "
                            f"If used as a filter, verify the value exists in data."
                        ),
                    )
                )

    severity_order = {"error": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda x: (severity_order[x.severity], x.measure, x.category))
    return issues
