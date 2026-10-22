from __future__ import annotations

import glob
import os
import re

from model.model_quality import find_model_dir, read_text_cached


def _read(path: str) -> str:
    return read_text_cached(path)


def _name(token: str) -> str:
    return token.strip().strip("'")


def _cell(text: str) -> str:
    return " ".join(str(text or "").split()).replace("|", "\\|")


def _anchor(text: str) -> str:
    return (
        re.sub(r"[^a-z0-9\- ]", "", str(text or "").lower()).strip().replace(" ", "-")
    )


_STRIP = re.compile(r'"(?:[^"]|"")*"|//[^\n]*|--[^\n]*|/\*.*?\*/', re.DOTALL)


def _mask_dax(expr: str) -> str:

    def _blank(m: re.Match) -> str:
        tok = m.group(0)
        if tok.startswith('"'):
            return '"' + " " * (len(tok) - 2) + '"'
        return " " * len(tok)

    return _STRIP.sub(_blank, expr or "")


def _drop_comments(expr: str) -> str:
    return _STRIP.sub(
        lambda m: m.group(0) if m.group(0).startswith('"') else " ", expr or ""
    )


def _parse_table(text: str) -> dict:
    tname = ""
    tm = re.search(r"^table\s+(?:'([^']+)'|(\S+))\s*$", text, re.MULTILINE)
    if tm:
        tname = tm.group(1) or tm.group(2)

    columns = []
    for cm in re.finditer(
        r"^\s+column\s+(?:'([^']+)'|(\S+))(.*?)(?=\n\s+(?:column|measure|hierarchy|partition)\b|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    ):
        cname = cm.group(1) or cm.group(2)
        dt = re.search(r"dataType:\s*(\w+)", cm.group(3) or "")
        columns.append((cname, dt.group(1) if dt else ""))

    measures = []
    for mm in re.finditer(
        r"^\s+measure\s+(?:'([^']+)'|([^=\s]+))\s*=\s*(.*?)"
        r"(?=\n\s+(?:measure|column|hierarchy|partition)\b|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    ):
        mname = mm.group(1) or mm.group(2)
        block = mm.group(0)
        expr_m = re.match(r"\s*(.*?)(?=\n\s+\w+:|\Z)", mm.group(3) or "", re.DOTALL)
        expr = (expr_m.group(1) if expr_m else mm.group(3) or "").strip()
        fmt = re.search(r"formatString:\s*(.+)", block)
        desc = re.search(r"///\s*(.+)", block)
        measures.append(
            (
                mname,
                " ".join(_drop_comments(expr).split()),
                fmt.group(1).strip() if fmt else "",
                desc.group(1).strip() if desc else "",
            )
        )
    return {"name": tname, "columns": columns, "measures": measures}


def _parse_relationships(text: str) -> list[tuple[str, str, str]]:
    rels = []
    for block in re.finditer(
        r"relationship\s+\w+.*?(?=\nrelationship\s|\Z)", text, re.DOTALL
    ):
        b = block.group(0)
        frm = re.search(r"fromColumn:\s*([^\n]+)", b)
        to = re.search(r"toColumn:\s*([^\n]+)", b)
        card = re.search(r"cardinality:\s*(\w+)", b)
        if frm and to:
            rels.append(
                (
                    frm.group(1).strip(),
                    to.group(1).strip(),
                    card.group(1) if card else "manyToOne",
                )
            )
    return rels


def _load_model(model_dir: str, model_name: str = "") -> tuple[str, list, list]:
    resolved = find_model_dir(model_dir) or model_dir
    defn = os.path.join(resolved, "definition")
    base = defn if os.path.isdir(defn) else resolved

    table_files = sorted(glob.glob(os.path.join(base, "tables", "*.tmdl")))
    if not table_files:
        table_files = sorted(
            glob.glob(os.path.join(base, "**", "*.tmdl"), recursive=True)
        )

    tables = [_parse_table(_read(f)) for f in table_files]
    tables = [t for t in tables if t["name"]]
    rels = _parse_relationships(_read(os.path.join(base, "relationships.tmdl")))
    title = model_name or os.path.basename(resolved).replace(".SemanticModel", "")
    return title, tables, rels


def _all_measures(tables: list) -> list[tuple]:
    return [
        (t["name"], n, e, fmt, desc)
        for t in tables
        for n, e, fmt, desc in t["measures"]
    ]


def _ref_table(ref: str) -> str:
    ref = (ref or "").strip()
    if "[" in ref:
        ref = ref.split("[", 1)[0]
    else:
        ref = ref.rpartition(".")[0] or ref
    return ref.strip().strip("'").strip()


def _is_fact(table: dict, rels: list) -> bool:
    name = table["name"]
    return any(_ref_table(frm) == name for frm, _to, _c in rels)


def generate_docs(model_dir: str, model_name: str = "") -> str:
    title, tables, rels = _load_model(model_dir, model_name)
    n_cols = sum(len(t["columns"]) for t in tables)
    n_meas = sum(len(t["measures"]) for t in tables)

    out = [f"# Data Dictionary — {title}", ""]
    out.append(
        f"_{len(tables)} tables · {n_cols} columns · {n_meas} measures · "
        f"{len(rels)} relationships_"
    )
    out.append("")
    out.append(
        "Each table below lists its columns (with data types) and its measures "
        "(with format and description). Full DAX for every measure lives in the "
        "measure specification; this dictionary is the scannable object index."
    )
    out.append("")
    if len(tables) > 1:
        out.append(
            "**Tables:** "
            + " · ".join(f"[{t['name']}](#{_anchor(t['name'])})" for t in tables)
        )
        out.append("")
    out.append("## Tables")
    out.append("")
    for t in tables:
        out.append(f"### {t['name']}")
        if t["columns"]:
            out.append("")
            out.append("| Column | Type |")
            out.append("|---|---|")
            for c, dt in t["columns"]:
                out.append(f"| {_cell(c)} | {_cell(dt) or '—'} |")
        if t["measures"]:
            out.append("")
            out.append("**Measures**")
            out.append("")
            out.append("| Measure | Format | Description |")
            out.append("|---|---|---|")
            for n, e, fmt, desc in t["measures"]:
                out.append(
                    f"| {_cell(n)} | {_cell(fmt) or '—'} | {_cell(desc) or '—'} |"
                )
        out.append("")
    if rels:
        out.append("## Relationships")
        out.append("")
        out.append("| From | To | Cardinality |")
        out.append("|---|---|---|")
        for frm, to, card in rels:
            out.append(f"| {_cell(frm)} | {_cell(to)} | {_cell(card)} |")
        out.append("")
    return "\n".join(out)


def generate_tech_design(model_dir: str, model_name: str = "") -> str:
    title, tables, rels = _load_model(model_dir, model_name)
    facts = [t for t in tables if _is_fact(t, rels)]
    dims = [t for t in tables if not _is_fact(t, rels)]
    measures = _all_measures(tables)

    out = [f"# Technical Design — {title}", ""]
    out.append("## 1. Overview")
    out.append("")
    out.append(
        f"Tabular semantic model with **{len(tables)} tables** "
        f"({len(facts)} fact-side, {len(dims)} dimension-side), "
        f"**{len(measures)} measures**, and **{len(rels)} relationships**."
    )
    out.append("")
    out.append("## 2. Schema")
    out.append("")
    out.append(
        "Tables on the *many* side of a relationship are classified fact-side; "
        "the rest are dimension-side."
    )
    out.append("")
    out.append("| Table | Role | Columns | Measures |")
    out.append("|---|---|---|---|")
    for t in tables:
        role = "Fact" if _is_fact(t, rels) else "Dimension"
        out.append(
            f"| {t['name']} | {role} | {len(t['columns'])} | {len(t['measures'])} |"
        )
    out.append("")
    out.append("## 3. Relationships")
    out.append("")
    if rels:
        out.append("| From (many) | To (one) | Cardinality |")
        out.append("|---|---|---|")
        for frm, to, card in rels:
            out.append(f"| {frm} | {to} | {card} |")
    else:
        out.append("_No relationships defined — every table is currently isolated._")
    out.append("")
    out.append("## 4. Measure inventory")
    out.append("")
    folders: dict[str, int] = {}
    for _t, _n, _e, _f, _d in measures:
        folders[_t] = folders.get(_t, 0) + 1
    out.append("| Home table | Measures |")
    out.append("|---|---|")
    for k, v in sorted(folders.items()):
        out.append(f"| {k} | {v} |")
    out.append("")
    out.append("## 5. Open items")
    out.append("")
    no_fmt = [f"{t}[{n}]" for t, n, _e, fmt, _d in measures if not fmt]
    no_desc = [f"{t}[{n}]" for t, n, _e, _f, desc in measures if not desc]
    out.append(
        f"- Measures without a format string: **{len(no_fmt)}**"
        + (
            f" — {', '.join(no_fmt[:8])}{'…' if len(no_fmt) > 8 else ''}"
            if no_fmt
            else ""
        )
    )
    out.append(f"- Measures without a description: **{len(no_desc)}**")
    if not rels:
        out.append("- **No relationships** — the model cannot filter across tables.")
    return "\n".join(out)


def generate_measure_spec(model_dir: str, model_name: str = "") -> str:
    from model.dax_preflight import extract_references

    title, tables, _rels = _load_model(model_dir, model_name)
    measures = _all_measures(tables)

    out = [f"# Measure Specification — {title}", "", f"_{len(measures)} measures_", ""]
    for tname, name, expr, fmt, desc in sorted(measures, key=lambda m: (m[0], m[1])):
        out.append(f"## {name}")
        out.append("")
        out.append(f"- **Home table:** {tname}")
        out.append(f"- **Format:** {fmt or '— (none set)'}")
        out.append(f"- **Description:** {desc or '— (none set)'}")
        cols, meas = extract_references(expr)
        deps = [f"{t}[{c}]" for t, c in dict.fromkeys(cols)] + [
            f"[{m}]" for m in dict.fromkeys(meas)
        ]
        out.append(
            f"- **Depends on:** {', '.join(deps) if deps else '— (literals only)'}"
        )
        out.append("")
        out.append("```dax")
        out.append(expr or "(empty)")
        out.append("```")
        out.append("")
    return "\n".join(out)


def generate_business_rules(model_dir: str, model_name: str = "") -> str:
    title, tables, rels = _load_model(model_dir, model_name)
    measures = _all_measures(tables)

    out = [f"# Business Rules — {title}", ""]
    out.append(
        "_Extracted from the model definition. Each rule is a business assumption "
        "encoded in DAX — confirm each with the process owner._"
    )
    out.append("")

    out.append("## Filter rules (a measure counts only some rows)")
    out.append("")
    rows = []
    for tname, name, expr, _f, _d in measures:
        body = _mask_dax(
            expr
        )  # a commented-out or string-embedded comparison is not a rule
        for m in re.finditer(
            r"('[^']+'|\b[\w]+)?\s*\[([^\]]+)\]\s*(=|<>|>=|<=|>|<|IN)\s*"
            r"(\"[^\"]*\"|\{[^}]*\}|[\d.]+)",
            body,
        ):
            src = (m.group(1) or "").strip()
            subject = f"{src}[{m.group(2)}]" if src else f"[{m.group(2)}]"
            value = (expr or "")[m.start(4) : m.end(4)]  # real literal, pre-mask
            rows.append((name, tname, f"{subject} {m.group(3)} {value}"))
    rows = list(dict.fromkeys(rows))  # the same rule can appear twice in one expr
    if rows:
        out.append("| Measure | Home table | Rule |")
        out.append("|---|---|---|")
        for name, tname, rule in rows:
            out.append(f"| {_cell(name)} | {_cell(tname)} | {_cell(rule)} |")
    else:
        out.append("_No literal filter rules found._")
    out.append("")

    out.append("## Ratio definitions")
    out.append("")
    ratios = [
        (n, e)
        for _t, n, e, _f, _d in measures
        if re.search(r"\bDIVIDE\s*\(", _mask_dax(e), re.IGNORECASE)
    ]
    if ratios:
        out.append("| Measure | Definition |")
        out.append("|---|---|")
        for n, e in ratios:
            disp = (e[:150] + "…") if len(e) > 150 else e
            out.append(f"| {_cell(n)} | `{_cell(disp)}` |")
    else:
        out.append("_No ratio measures found._")
    out.append("")

    out.append("## Grain & joins")
    out.append("")
    if rels:
        for frm, to, card in rels:
            out.append(f"- `{frm}` → `{to}` ({card}) — filters flow one → many.")
    else:
        out.append("_No relationships defined._")
    return "\n".join(out)


def generate_testing_checklist(model_dir: str, model_name: str = "") -> str:
    title, tables, rels = _load_model(model_dir, model_name)
    measures = _all_measures(tables)

    out = [f"# Testing Checklist — {title}", ""]
    out.append("## 1. Measure correctness")
    out.append("")
    for tname, name, _e, fmt, _d in sorted(measures, key=lambda m: m[1]):
        out.append(
            f"- [ ] `{name}` ({tname}) returns a non-blank, plausible value at "
            f"total level{'' if fmt else ' — ⚠ no format string set'}"
        )
    out.append("")
    out.append("## 2. Relationship integrity")
    out.append("")
    if rels:
        for frm, to, _c in rels:
            out.append(
                f"- [ ] `{frm}` → `{to}`: no orphan keys (every value on the many "
                f"side exists on the one side)"
            )
    else:
        out.append("- [ ] ⚠ No relationships defined — verify this is intentional")
    out.append("")
    out.append("## 3. Time intelligence")
    out.append("")
    ti = [
        n
        for _t, n, e, _f, _d in measures
        if re.search(
            r"SAMEPERIODLASTYEAR|DATEADD|DATESYTD|TOTALYTD|DATESINPERIOD", e or "", re.IGNORECASE
        )
    ]
    if ti:
        out.append("- [ ] A date table exists and is marked as a date table")
        for n in ti:
            out.append(f"- [ ] `{n}` returns correct values across a year boundary")
    else:
        out.append("_No time-intelligence measures detected._")
    out.append("")
    out.append("## 4. Report-level")
    out.append("")
    out.append("- [ ] Every visual renders without an error card")
    out.append("- [ ] Slicers filter every visual on the page as expected")
    out.append("- [ ] Totals match the source system for one agreed reference period")
    out.append("- [ ] Report opens cleanly in Power BI Desktop (no Missing_References)")
    return "\n".join(out)


def generate_deployment_notes(model_dir: str, model_name: str = "") -> str:
    title, tables, rels = _load_model(model_dir, model_name)
    measures = _all_measures(tables)

    out = [f"# Deployment Notes — {title}", ""]
    out.append("## Contents")
    out.append("")
    out.append(f"- Tables: **{len(tables)}**")
    out.append(f"- Measures: **{len(measures)}**")
    out.append(f"- Relationships: **{len(rels)}**")
    out.append("")
    out.append("## Pre-deployment")
    out.append("")
    out.append("- [ ] Target workspace and semantic model name confirmed")
    out.append("- [ ] Data source credentials configured in the target workspace")
    out.append("- [ ] Existing model backed up (export TMDL/TMSL before overwriting)")
    out.append("- [ ] Testing checklist signed off")
    out.append("")
    out.append("## Deploy")
    out.append("")
    out.append("- [ ] Publish the semantic model (TMDL import / XMLA deploy)")
    out.append("- [ ] Trigger a full refresh and confirm it succeeds")
    out.append("- [ ] Publish the report and confirm it binds to the new model")
    out.append("")
    out.append("## Post-deployment")
    out.append("")
    out.append("- [ ] Spot-check totals against the source for one reference period")
    out.append("- [ ] Confirm RLS roles (if any) return the expected rows per role")
    out.append("- [ ] Set the refresh schedule")
    out.append("- [ ] Grant access to the consumer audience")
    out.append("")
    out.append("## Rollback")
    out.append("")
    out.append("- [ ] Re-import the pre-deployment TMDL/TMSL backup")
    out.append("- [ ] Re-run a refresh and confirm the prior numbers return")
    return "\n".join(out)


def generate_release_notes(model_dir: str, model_name: str = "") -> str:
    title, tables, rels = _load_model(model_dir, model_name)
    measures = _all_measures(tables)

    out = [f"# Release Notes — {title}", ""]
    out.append("> Draft — seeded from the model's current contents. Edit the")
    out.append("> summary and move unchanged items out before publishing.")
    out.append("")
    out.append("## Summary")
    out.append("")
    out.append(
        f"This release contains {len(tables)} tables, {len(measures)} measures and "
        f"{len(rels)} relationships."
    )
    out.append("")
    out.append("## Measures in this release")
    out.append("")
    for tname, name, _e, _f, desc in sorted(measures, key=lambda m: m[1]):
        out.append(f"- **{name}** ({tname}) — {desc or 'no description'}")
    out.append("")
    out.append("## Tables in this release")
    out.append("")
    for t in tables:
        role = "fact" if _is_fact(t, rels) else "dimension"
        out.append(f"- **{t['name']}** ({role}, {len(t['columns'])} columns)")
    out.append("")
    out.append("## Known issues")
    out.append("")
    no_fmt = [n for _t, n, _e, fmt, _d in measures if not fmt]
    if no_fmt:
        out.append(
            f"- {len(no_fmt)} measure(s) have no format string: {', '.join(no_fmt[:10])}"
        )
    if not rels:
        out.append("- No relationships are defined in this model.")
    if not no_fmt and rels:
        out.append("- None recorded.")
    return "\n".join(out)


DOC_TYPES = {
    "data_dictionary": generate_docs,
    "tech_design": generate_tech_design,
    "measure_spec": generate_measure_spec,
    "business_rules": generate_business_rules,
    "testing_checklist": generate_testing_checklist,
    "deployment_notes": generate_deployment_notes,
    "release_notes": generate_release_notes,
}

DOC_FILENAMES = {
    "data_dictionary": "model_docs.md",
    "tech_design": "technical_design.md",
    "measure_spec": "measure_specification.md",
    "business_rules": "business_rules.md",
    "testing_checklist": "testing_checklist.md",
    "deployment_notes": "deployment_notes.md",
    "release_notes": "release_notes.md",
}


def generate_doc(
    model_dir: str, doc_type: str = "data_dictionary", model_name: str = ""
) -> str:
    fn = DOC_TYPES.get(doc_type)
    if fn is None:
        return (
            f"ERROR — unknown doc_type {doc_type!r}. "
            f"Valid: {', '.join(sorted(DOC_TYPES))}."
        )
    return fn(model_dir, model_name)
