
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

SEVERITY_NAME = {1: "info", 2: "warning", 3: "error"}

# Text-ish TMSCHEMA data types. A text column that aggregates is almost always a
# modelling slip: Power BI offers Sum/Count on it and analysts take the offer.
_TEXT_TYPES = {"string", "text", "binary"}

# SummarizeBy values that mean "does not aggregate".
_NO_AGG = {"none", "default", ""}


@dataclass
class Finding:
    rule: str
    severity: str
    objects: list[str] = field(default_factory=list)


def _norm(v) -> str:
    return ("" if v is None else str(v)).strip()


def _lower(v) -> str:
    return _norm(v).lower()


def _blank(v) -> bool:
    return not _norm(v)


def _strip_dax_noise(dax: str) -> str:
    s = _norm(dax)
    s = re.sub(r'"(?:[^"]|"")*"', " ", s)  # string literals
    s = re.sub(r"//[^\n]*", " ", s)  # line comments
    s = re.sub(r"/\*.*?\*/", " ", s, flags=re.DOTALL)  # block comments
    return s


def _relationship_columns(rels) -> set[tuple[str, str]]:
    used: set[tuple[str, str]] = set()
    for r in rels:
        used.add((_norm(r.get("from_table")), _norm(r.get("from_column"))))
        used.add((_norm(r.get("to_table")), _norm(r.get("to_column"))))
    return used


# --- Rules ------------------------------------------------------------------
# Each returns a list of object paths that violate it. Kept small and separate
# so a false positive can be fixed and regression-tested in isolation.


def _r_measure_format_string(meta) -> list[str]:
    return [
        f"{_norm(m.get('table'))}[{_norm(m.get('name'))}]"
        for m in meta.get("measures", [])
        if not m.get("is_hidden") and _blank(m.get("format_string"))
    ]


def _r_measure_no_expression(meta) -> list[str]:
    out = []
    for m in meta.get("measures", []):
        expr = _norm(m.get("expression")).upper()
        if not expr or expr == "BLANK()":
            out.append(f"{_norm(m.get('table'))}[{_norm(m.get('name'))}]")
    return out


def _r_measure_display_folder(meta) -> list[str]:
    return [
        f"{_norm(m.get('table'))}[{_norm(m.get('name'))}]"
        for m in meta.get("measures", [])
        if not m.get("is_hidden") and _blank(m.get("display_folder"))
    ]


def _r_measure_description(meta) -> list[str]:
    return [
        f"{_norm(m.get('table'))}[{_norm(m.get('name'))}]"
        for m in meta.get("measures", [])
        if not m.get("is_hidden") and _blank(m.get("description"))
    ]


def _r_measure_divide(meta) -> list[str]:
    out = []
    for m in meta.get("measures", []):
        expr = _strip_dax_noise(m.get("expression"))
        if not expr:
            continue
        # Bare division only; DIVIDE() anywhere in the expression clears it.
        if "/" in expr and not re.search(r"\bDIVIDE\s*\(", expr, re.IGNORECASE):
            out.append(f"{_norm(m.get('table'))}[{_norm(m.get('name'))}]")
    return out


def _r_measure_duplicate_expression(meta) -> list[str]:
    by_expr: dict[str, list[str]] = defaultdict(list)
    for m in meta.get("measures", []):
        # Fingerprint, not display: drop ALL whitespace and case so measures
        # that differ only in formatting still collide. String literals are
        # already stripped, so this cannot merge two different literals. DAX
        # identifiers and functions are case-insensitive, so folding case is
        # safe and catches SUM(x) vs Sum(x).
        expr = re.sub(r"\s+", "", _strip_dax_noise(m.get("expression"))).upper()
        if not expr:
            continue
        by_expr[expr].append(f"{_norm(m.get('table'))}[{_norm(m.get('name'))}]")
    out: list[str] = []
    for names in by_expr.values():
        if len(names) > 1:
            out.append(" == ".join(sorted(names)))
    return out


def _r_column_hide_fk(meta) -> list[str]:
    used = _relationship_columns(meta.get("relationships", []))
    return [
        f"{_norm(c.get('table'))}[{_norm(c.get('name'))}]"
        for c in meta.get("columns", [])
        if (_norm(c.get("table")), _norm(c.get("name"))) in used
        and not c.get("is_hidden")
        and not c.get("is_key")
    ]


def _r_column_text_aggregated(meta) -> list[str]:
    out = []
    for c in meta.get("columns", []):
        if _lower(c.get("data_type")) not in _TEXT_TYPES:
            continue
        if _lower(c.get("summarize_by")) in _NO_AGG:
            continue
        out.append(f"{_norm(c.get('table'))}[{_norm(c.get('name'))}]")
    return out


def _r_relationship_bidirectional(meta) -> list[str]:
    out = []
    for r in meta.get("relationships", []):
        cf = _lower(r.get("cross_filter"))
        if cf in ("both", "bothdirections", "2"):
            out.append(
                f"{_norm(r.get('from_table'))}[{_norm(r.get('from_column'))}] -> "
                f"{_norm(r.get('to_table'))}[{_norm(r.get('to_column'))}]"
            )
    return out


def _r_relationship_many_to_many(meta) -> list[str]:
    out = []
    for r in meta.get("relationships", []):
        if (
            _lower(r.get("from_cardinality")) == "many"
            and _lower(r.get("to_cardinality")) == "many"
        ):
            out.append(
                f"{_norm(r.get('from_table'))}[{_norm(r.get('from_column'))}] -> "
                f"{_norm(r.get('to_table'))}[{_norm(r.get('to_column'))}]"
            )
    return out


def _r_relationship_inactive_unused(meta) -> list[str]:
    all_dax = " ".join(
        _strip_dax_noise(m.get("expression")) for m in meta.get("measures", [])
    ).upper()
    out = []
    for r in meta.get("relationships", []):
        if r.get("is_active", True):
            continue
        from_col = _norm(r.get("from_column")).upper()
        to_col = _norm(r.get("to_column")).upper()
        if "USERELATIONSHIP" in all_dax and (from_col in all_dax or to_col in all_dax):
            continue
        out.append(
            f"{_norm(r.get('from_table'))}[{_norm(r.get('from_column'))}] -> "
            f"{_norm(r.get('to_table'))}[{_norm(r.get('to_column'))}]"
        )
    return out


def _r_table_no_relationships(meta) -> list[str]:
    related: set[str] = set()
    for r in meta.get("relationships", []):
        related.add(_norm(r.get("from_table")))
        related.add(_norm(r.get("to_table")))
    out = []
    for t in meta.get("tables", []):
        name = _norm(t.get("name"))
        if t.get("is_hidden") or not name or name in related:
            continue
        if name.lower() in {"dimdate", "datetable", "date", "calendar"}:
            continue
        if name.startswith("_"):
            continue
        out.append(name)
    return out


RULES = [
    (
        "MEASURES_NO_DAX_EXPRESSION",
        "Measure has empty DAX expression",
        3,
        _r_measure_no_expression,
    ),
    (
        "RELATIONSHIPS_USE_DISCOURAGED_BIDI",
        "Avoid bi-directional relationships",
        3,
        _r_relationship_bidirectional,
    ),
    (
        "RELATIONSHIPS_MANY_TO_MANY",
        "Avoid many-to-many relationships",
        3,
        _r_relationship_many_to_many,
    ),
    (
        "MEASURES_HAVE_FORMAT_STRING",
        "Measures should have a format string",
        2,
        _r_measure_format_string,
    ),
    (
        "MEASURES_USE_DIVIDE_NOT_SLASH",
        "DAX should use DIVIDE() not /",
        2,
        _r_measure_divide,
    ),
    ("COLUMNS_HIDE_FK", "Foreign-key columns should be hidden", 2, _r_column_hide_fk),
    (
        "COLUMNS_TEXT_SHOULD_NOT_AGGREGATE",
        "Text columns should not summarise",
        2,
        _r_column_text_aggregated,
    ),
    (
        "RELATIONSHIPS_INACTIVE_UNUSED",
        "Inactive relationship never activated by USERELATIONSHIP",
        2,
        _r_relationship_inactive_unused,
    ),
    (
        "TABLES_NO_RELATIONSHIPS",
        "Table has no relationships",
        2,
        _r_table_no_relationships,
    ),
    (
        "MEASURES_DISPLAY_FOLDER",
        "Measures should live in a display folder",
        1,
        _r_measure_display_folder,
    ),
    (
        "MEASURES_HAVE_DESCRIPTION",
        "Measures should have a description",
        1,
        _r_measure_description,
    ),
    (
        "MEASURES_DUPLICATE_EXPRESSION",
        "Measures share an identical expression",
        1,
        _r_measure_duplicate_expression,
    ),
]


# Rules evaluable from a local PBIP/TMDL read. The reference loader does not
# expose IsKey, SummarizeBy, measure IsHidden or descriptions, and a rule that
# reads a missing field as False produces confident false positives — so those
# rules are simply not run locally rather than run on partial data.
LOCAL_SAFE_RULES = {
    "MEASURES_NO_DAX_EXPRESSION",
    "MEASURES_HAVE_FORMAT_STRING",
    "MEASURES_DISPLAY_FOLDER",
    "MEASURES_USE_DIVIDE_NOT_SLASH",
    "MEASURES_DUPLICATE_EXPRESSION",
    "RELATIONSHIPS_USE_DISCOURAGED_BIDI",
    "RELATIONSHIPS_INACTIVE_UNUSED",
    "TABLES_NO_RELATIONSHIPS",
}


def from_reference_model(ref) -> dict:
    tables = [
        {
            "name": t.name,
            "is_hidden": bool(getattr(t, "is_hidden", False)),
            "description": "",
        }
        for t in getattr(ref, "business_tables", []) or []
    ]
    measures = [
        {
            "table": getattr(m, "host_table", ""),
            "name": m.name,
            "expression": getattr(m, "dax", "") or "",
            "format_string": getattr(m, "format_string", "") or "",
            "display_folder": getattr(m, "display_folder", "") or "",
            "is_hidden": False,
            "description": "",
        }
        for m in getattr(ref, "measures", []) or []
    ]
    columns = [
        {
            "table": t.name,
            "name": c.name,
            "data_type": getattr(c, "data_type", "") or "",
            "is_hidden": bool(getattr(c, "is_hidden", False)),
        }
        for t in getattr(ref, "business_tables", []) or []
        for c in getattr(t, "columns", []) or []
    ]
    relationships = [
        {
            "from_table": r.from_table,
            "from_column": r.from_column,
            "to_table": r.to_table,
            "to_column": r.to_column,
            "is_active": bool(getattr(r, "is_active", True)),
            "cross_filter": getattr(r, "cross_filter", "") or "",
        }
        for r in getattr(ref, "relationships", []) or []
    ]
    return {
        "tables": tables,
        "measures": measures,
        "columns": columns,
        "relationships": relationships,
    }


def evaluate(meta: dict, only: set | None = None) -> dict:
    meta = meta or {}
    findings: list[Finding] = []
    # A rule that threw did not pass — it never ran. It stays out of the
    # severity buckets (a crashed rule is not a modelling fault) but the caller
    # gets it on its own key, because "12 of 12 rules, 0 errors" read off a run
    # where three rules died is a clean bill of health nobody earned.
    errored: list[dict] = []
    for rule_id, name, severity, fn in RULES:
        if only is not None and rule_id not in only:
            continue
        try:
            objects = fn(meta)
        except Exception as exc:  # one broken rule must not lose the other 11
            detail = f"{type(exc).__name__}: {exc}"
            errored.append({"rule": rule_id, "error": detail})
            findings.append(Finding(f"{rule_id} (rule errored)", "info", [detail]))
            continue
        if objects:
            findings.append(Finding(name, SEVERITY_NAME[severity], sorted(objects)))

    n_run = len(RULES) if only is None else len([r for r in RULES if r[0] in only])
    errors = [f for f in findings if f.severity == "error"]
    warnings = [f for f in findings if f.severity == "warning"]
    infos = [f for f in findings if f.severity == "info"]
    flagged = sum(len(f.objects) for f in findings)
    summary = (
        f"{len(errors)} error rules · {len(warnings)} warning rules · "
        f"{len(infos)} info rules ({flagged} objects flagged) "
        f"[{n_run - len(errored)} of {len(RULES)} rules evaluated in-process"
    )
    summary += (
        f"; {len(errored)} FAILED TO RUN — this model is NOT fully checked]"
        if errored
        else "]"
    )
    return {
        "available": True,
        "engine": "fabricbi-python-bpa",
        "errors": [vars(f) for f in errors],
        "warnings": [vars(f) for f in warnings],
        "info": [vars(f) for f in infos],
        "rules_errored": errored,
        "complete": not errored,
        "summary": summary,
    }
