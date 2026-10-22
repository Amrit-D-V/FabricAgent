from __future__ import annotations

import re

from model.dax_preflight import extract_references

_FUNC_NOTES = {
    "CALCULATE": "changes the filter context (the core of most measures)",
    "CALCULATETABLE": "returns a table with a modified filter context",
    "FILTER": "row-by-row table filter (an iterator — slower than a predicate)",
    "ALL": "removes filters (used for totals / % of total)",
    "ALLEXCEPT": "removes filters except the listed columns",
    "ALLSELECTED": "respects the user's slicer selection, ignores inner filters",
    "VALUES": "distinct values of a column in the current context",
    "DISTINCTCOUNT": "count of distinct values (expensive on high-cardinality)",
    "COUNTROWS": "row count of a table",
    "DIVIDE": "safe division (returns BLANK instead of an error on zero)",
    "SUMX": "row-by-row sum (iterator)",
    "AVERAGEX": "row-by-row average (iterator)",
    "MAXX": "row-by-row max (iterator)",
    "MINX": "row-by-row min (iterator)",
    "RANKX": "ranks rows against a table expression",
    "TOPN": "top N rows by an expression",
    "SAMEPERIODLASTYEAR": "shifts the date filter back one year",
    "DATEADD": "shifts the date filter by an interval",
    "DATESYTD": "year-to-date date range",
    "DATESINPERIOD": "a rolling date window",
    "TOTALYTD": "year-to-date total",
    "LASTNONBLANKVALUE": "last non-blank value by date (semi-additive)",
    "USERELATIONSHIP": "activates an inactive relationship for this calculation",
    "RELATED": "follows a many-to-one relationship to the one side",
    "RELATEDTABLE": "follows a one-to-many relationship to the many side",
    "SUMMARIZE": "groups a table (prefer SUMMARIZECOLUMNS for new code)",
    "SUMMARIZECOLUMNS": "groups and aggregates (the modern grouping function)",
    "ADDCOLUMNS": "adds computed columns to a table expression",
    "SELECTCOLUMNS": "projects specific columns from a table",
    "COALESCE": "first non-blank of its arguments",
    "SWITCH": "multi-branch conditional",
    "VAR": "stores an intermediate result (evaluated once — good for perf)",
}

_STRIP = re.compile(r'"(?:[^"]|"")*"|//[^\n]*|--[^\n]*|/\*.*?\*/', re.DOTALL)


def _funcs_used(expr: str) -> list[str]:
    body = _STRIP.sub(" ", expr or "")
    found = {
        m.group(1).upper()
        for m in re.finditer(r"\b([A-Za-z][A-Za-z0-9._]*)\s*\(", body)
    }
    return sorted(found)


def explain_dax(expression: str, col_reg: dict, mea_reg: dict) -> str:
    if not (expression or "").strip():
        return "ERROR — pass a DAX `expression` to explain."

    cols, meas = extract_references(expression)
    col_keys = {k for k in (col_reg or {}) if k != "_meta"}
    mea_keys = {k for k in (mea_reg or {}) if k != "_meta"}
    known_measures = {k.rpartition(".")[2] for k in mea_keys}
    verified = bool(col_keys or mea_keys)

    out = ["DAX EXPLANATION", ""]

    lines = []
    for table, col in dict.fromkeys(cols):
        key = f"{table}.{col}"
        if key in mea_keys:
            kind = "measure"
        elif key in col_keys:
            kind = "column"
        elif col in known_measures:
            kind = "measure (referenced with its table)"
        elif not verified:
            kind = "not verified (model metadata not loaded)"
        else:
            kind = "NOT FOUND in the model"
        lines.append(f"  • {table}[{col}] — {kind}")
    for name in dict.fromkeys(meas):
        if name in known_measures:
            hit = "measure"
        elif not verified:
            hit = "not verified (model metadata not loaded)"
        else:
            hit = "NOT FOUND in the model"
        lines.append(f"  • [{name}] — {hit}")
    out.append("References:")
    out.extend(lines or ["  (none — the expression uses only literals/functions)"])
    out.append("")

    funcs = _funcs_used(expression)
    noted = [(f, _FUNC_NOTES[f]) for f in funcs if f in _FUNC_NOTES]
    if noted:
        out.append("Functions:")
        out.extend(f"  • {f} — {note}" for f, note in noted)
        other = [f for f in funcs if f not in _FUNC_NOTES]
        if other:
            out.append(f"  • also uses: {', '.join(other)}")
        out.append("")

    fset = set(funcs)
    shape = []
    if fset & {"CALCULATE", "CALCULATETABLE"}:
        shape.append("modifies filter context (CALCULATE)")
    if fset & {"SUMX", "AVERAGEX", "MAXX", "MINX", "COUNTX", "RANKX", "FILTER"}:
        shape.append("iterates row-by-row (iterator — cost scales with rows)")
    if fset & {
        "SAMEPERIODLASTYEAR",
        "DATEADD",
        "DATESYTD",
        "DATESINPERIOD",
        "TOTALYTD",
        "TOTALMTD",
        "TOTALQTD",
        "PARALLELPERIOD",
    }:
        shape.append("time intelligence (requires a marked date table)")
    if fset & {"ALL", "ALLEXCEPT", "ALLSELECTED"}:
        shape.append("removes/alters filters (ratio or total pattern)")
    if "USERELATIONSHIP" in fset:
        shape.append("activates an inactive relationship")
    if "VAR" in (expression or "").upper():
        shape.append("uses VAR (intermediate results evaluated once)")
    if shape:
        out.append("Shape:")
        out.extend(f"  • {s}" for s in shape)
        out.append("")

    missing = [ln for ln in lines if "NOT FOUND" in ln]
    if missing:
        out.append(
            f"⚠ {len(missing)} reference(s) do not exist in the connected model."
        )
    elif lines and not verified:
        out.append(
            "Reference verification skipped - model metadata not loaded, so "
            "existence was not checked against the model."
        )
    return "\n".join(out).rstrip()


def dax_smells(expression: str) -> list[tuple[str, str]]:
    if not (expression or "").strip():
        return []

    body = _STRIP.sub(" ", expression)
    up = body.upper()
    findings: list[tuple[str, str]] = []

    if re.search(r"\)\s*/\s*[\[(]|\]\s*/\s*[\[(]", body):
        findings.append(
            (
                "Bare `/` division",
                ("Use DIVIDE(numerator, denominator) — it returns BLANK on divide-by-zero "
                "instead of an error."),
            )
        )
    if re.search(r"COUNTROWS\s*\(\s*FILTER\s*\(", up):
        findings.append(
            (
                "COUNTROWS(FILTER(...))",
                ("Rewrite as CALCULATE(COUNTROWS('Table'), <predicate>) — the engine can "
                "push a predicate into the storage engine; FILTER forces a row-by-row scan."),
            )
        )
    if re.search(r"FILTER\s*\(\s*ALL\s*\(", up):
        findings.append(
            (
                "FILTER(ALL(...))",
                ("If you only need to drop filters on specific columns, ALLEXCEPT or "
                "REMOVEFILTERS is cheaper than materializing the whole table."),
            )
        )
    if re.search(
        r"CALCULATE\s*\(\s*[^,()]*\s*,\s*FILTER\s*\(\s*'?[\w ]+'?\s*,\s*'?[\w ]+'?\s*\[[^\]]+\]\s*(=|<>|>|<|>=|<=)",
        up,
    ):
        findings.append(
            (
                "FILTER with a simple predicate inside CALCULATE",
                ("A single-column comparison can be written directly: "
                "CALCULATE([M], 'T'[col] = \"x\") — no FILTER wrapper needed."),
            )
        )
    if re.search(r"IF\s*\(\s*ISBLANK\s*\(", up):
        findings.append(
            (
                "IF(ISBLANK(x), y, x)",
                "COALESCE(x, y) is shorter and evaluates x once.",
            )
        )
    if len(re.findall(r"\bDISTINCTCOUNT\s*\(", up)) and re.search(
        r"\b(SUMX|AVERAGEX|MAXX|MINX|FILTER)\s*\(", up
    ):
        findings.append(
            (
                "DISTINCTCOUNT inside an iterator",
                ("DISTINCTCOUNT is expensive on high-cardinality columns and re-runs per "
                "row here. Precompute with a VAR outside the iterator when possible."),
            )
        )
    calc_calls = re.findall(r"CALCULATE\s*\([^()]*(?:\([^()]*\)[^()]*)*\)", body, re.IGNORECASE)
    dupes = {c for c in calc_calls if calc_calls.count(c) > 1}
    if dupes:
        findings.append(
            (
                "Repeated identical sub-expression",
                ("The same CALCULATE(...) appears more than once — assign it to a VAR and "
                "reuse it so the engine evaluates it once (classic in YoY %% measures)."),
            )
        )
    if re.search(r"\bSUMMARIZE\s*\(", up) and "ADDCOLUMNS" not in up:
        findings.append(
            (
                "SUMMARIZE used for aggregation",
                ("Prefer SUMMARIZECOLUMNS (or ADDCOLUMNS over SUMMARIZE) — plain "
                "SUMMARIZE with aggregations has known context-transition pitfalls."),
            )
        )
    if re.search(r"\bCALCULATE\s*\(\s*[^,]+\)\s*$", body.strip(), re.IGNORECASE):
        findings.append(
            (
                "CALCULATE with no filter argument",
                ("CALCULATE([M]) only performs context transition — if that's intentional "
                "it's fine, otherwise the wrapper is redundant."),
            )
        )
    return findings


def optimize_dax(expression: str) -> str:
    if not (expression or "").strip():
        return "ERROR — pass a DAX `expression` to optimize."

    findings = dax_smells(expression)
    if not findings:
        return (
            "OPTIMIZE — no anti-patterns detected.\n"
            "Checked: bare division, COUNTROWS(FILTER()), FILTER(ALL()), FILTER with a "
            "simple predicate, IF(ISBLANK()), DISTINCTCOUNT in an iterator, repeated "
            "sub-expressions, SUMMARIZE aggregation, empty CALCULATE."
        )
    out = [f"OPTIMIZE — {len(findings)} issue(s):", ""]
    for i, (issue, fix) in enumerate(findings, 1):
        out.append(f"{i}. {issue}")
        out.append(f"   → {fix}")
    return "\n".join(out)


def debug_dax(
    expression: str,
    col_reg: dict,
    mea_reg: dict,
    server: str = "",
    database: str = "",
    table: str = "",
    otype: str = "measure",
) -> str:
    if not (expression or "").strip():
        return "ERROR — pass a DAX `expression` to debug."

    out = ["DAX DEBUG", ""]

    from model.dax_preflight import validate_dax_references

    ref_err = validate_dax_references(expression, col_reg, mea_reg)
    if ref_err:
        out.append("Reference check: FAILED")
        out.append(ref_err)
        out.append("")
        out.append("Fix the reference(s) above — the engine can't compile this yet.")
        return "\n".join(out)
    out.append("Reference check: passed (every table/column/measure exists).")

    lint = optimize_dax(expression)
    if not lint.startswith("OPTIMIZE — no anti-patterns"):
        out.append("")
        out.append(lint)

    if not (server and database):
        out.append("")
        out.append(
            "Live check: SKIPPED (not connected). Connect to the model to get the "
            "engine's actual error message."
        )
        return "\n".join(out)

    from model.dax_live_validate import validate_dax_live

    live, live_status = validate_dax_live(
        server, database, otype, table, "__debug_probe", expression,
        return_status=True,
    )
    out.append("")
    if live_status == "error":
        out.append("Live check: FAILED — the engine rejected it:")
        out.append(live)
    elif live_status == "infra":
        out.append(
            "Live check: INCONCLUSIVE — the engine probe hit an infrastructure "
            "error (timeout, auth, or connection issue), not a syntax check. This "
            "is NOT confirmation the expression is correct — treat it as unverified "
            "and retry the live check once the connection issue is resolved."
        )
    elif live_status == "skipped":
        out.append(
            "Live check: SKIPPED — no live probe could be built for this "
            "expression/object type."
        )
    else:
        out.append(
            "Live check: passed — the engine compiled and evaluated this expression. "
            "If the RESULT is still wrong, the problem is logic (filter context / "
            "relationship direction), not syntax."
        )
    return "\n".join(out)
