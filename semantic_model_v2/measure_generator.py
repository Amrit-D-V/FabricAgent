from __future__ import annotations

import json
import logging
import re
import time

from llm.llm_client import UsageInfo
from model.dax_preflight import extract_references

from ._llm import log_usage, stream_collect
from .plan import MeasureSpec, SchemaPlan

logger = logging.getLogger(__name__)


MEASURE_SYSTEM_PROMPT = """You are a DAX expert. Write ONE measure's DAX expression. Output strict JSON only.

## Input you receive
- measure_name: the measure's display name
- host_table: the table this measure belongs to (use it in column references)
- intent: business meaning of the measure
- template_dax: optional DAX skeleton from the canonical template library — adapt it
- host_columns: list of columns available on host_table with their data types
- referenced_tables: other tables that may be involved (with their columns)
- profile_sample_values: real values from the data — for filter literals

## Output

Return ONLY this JSON object — no prose, no markdown:

{
  "dax": "the DAX expression on a single line",
  "format_string": "TMDL formatString value (e.g. '#,##0' or '0.0%') or null"
}

## Rules

1. Column references use 'TableName'[ColumnName] form. For tables with dots (e.g. 'DEP.factpolicytransaction') ALWAYS single-quote.
2. Measure references use [Measure Name] form. DON'T quote the table on measure refs.
3. Filter LITERALS must match real values from profile_sample_values. If the user wants "Inforce" but the profile shows "Active"/"Pending"/"Expired", use "Active" — never invent a value.
4. NEVER use a column that isn't in host_columns or referenced_tables[*].columns.
5. Keep DAX on ONE line (no newlines). If logic requires VAR/RETURN, inline it: `VAR x = … RETURN …`.
6. For boolean filters in CALCULATE, use the comparison form: `'T'[col] = "value"`, never raw boolean columns.
7. PERCENTAGES & RATIOS (CRITICAL): In Power BI, `format_string: "0.0%"` automatically multiplies the underlying DAX result by 100. DAX expressions for percentages/ratios/rates/shares MUST evaluate to a fraction between `0.0` and `1.0` (e.g. `0.1936` for 19.36%). NEVER multiply by 100 in the DAX expression when format_string is `"0.0%"`. If the source column is already on a 0-100 scale (e.g. 19.36 or 75.0), divide by 100: `DIVIDE(AVERAGE('Table'[Col]), 100, 0)` or `AVERAGE('Table'[Col]) / 100`. NEVER use `SUM()` across percentages or rates; use `AVERAGE()` or `DIVIDE()`. All sibling percentage metrics must use `format_string: "0.0%"`.
8. Output ONLY the JSON object. No prose, no markdown fences.
"""


def _lint_dax(expr: str) -> list[str]:
    issues: list[str] = []
    if not expr or not expr.strip():
        issues.append("empty DAX expression")
        return issues

    stripped = re.sub(r'"(?:[^"\\]|\\.)*"', '""', expr)

    open_paren = stripped.count("(")
    close_paren = stripped.count(")")
    if open_paren != close_paren:
        issues.append(
            f"unbalanced parentheses ({open_paren} open vs {close_paren} close)"
        )
    open_brk = stripped.count("[")
    close_brk = stripped.count("]")
    if open_brk != close_brk:
        issues.append(f"unbalanced brackets ({open_brk} open vs {close_brk} close)")

    no_strings = stripped
    for m in re.finditer(r"\bFILTER\s*\(", no_strings, re.IGNORECASE):
        prefix = no_strings[: m.start()].rstrip()
        if not prefix or prefix.endswith((",", "(")):
            continue
        issues.append(
            "FILTER appears outside CALCULATE/argument position — likely a "
            "top-level FILTER paste mistake. Wrap in CALCULATE([measure], FILTER(...))."
        )
        break

    if re.search(r"\)\s*\[", no_strings):
        issues.append(
            "found `)[...]` pattern — looks like a misplaced paren. "
            "Column refs should be `Table[Col]`, not `FUNC(Table)[Col]`."
        )

    for fn in ("SUM", "AVERAGE", "MIN", "MAX", "COUNT", "DISTINCTCOUNT"):
        for m in re.finditer(rf"\b{fn}\s*\(\s*\[", no_strings, re.IGNORECASE):
            issues.append(
                f"{fn}(...) needs a table-qualified column reference like "
                f"`{fn}('Table'[Col])`, not just `{fn}([Col])`."
            )
            break

    return issues


def _reference_issues(dax: str, plan: SchemaPlan) -> list[str]:
    table_cols = {t.name: {c.name for c in t.columns} for t in plan.tables}
    measure_names = {m.name for m in plan.measures}
    columns, bare = extract_references(dax)
    issues: list[str] = []
    for tbl, col in columns:
        cols = table_cols.get(tbl)
        if cols is None:
            issues.append(
                f"references unknown table '{tbl}' - known tables: "
                f"{sorted(table_cols)}"
            )
        elif col not in cols and col not in measure_names:
            issues.append(
                f"references unknown column '{tbl}'[{col}] - available "
                f"columns on '{tbl}': {sorted(cols)[:8]}..."
            )
    for name in bare:
        if name not in measure_names:
            issues.append(
                f"references unknown measure '[{name}]' - known measures: "
                f"{sorted(measure_names)[:8]}..."
            )
    return list(dict.fromkeys(issues))


from util.json_utils import strip_json_fences as _strip_json


def _call_llm(
    system: str, user: str, max_tokens: int = 1500
) -> tuple[str, UsageInfo | None]:
    return stream_collect(system, user, agent="small_task", max_tokens=max_tokens)


def _load_templates() -> dict[str, str]:
    import os

    template_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "measure_templates",
    )
    if not os.path.isdir(template_dir):
        return {}

    out: dict[str, str] = {}
    files = sorted(
        [f for f in os.listdir(template_dir) if f.endswith(".md")],
        key=lambda f: (f == "generic.md", f),
    )
    for fname in files:
        path = os.path.join(template_dir, fname)
        try:
            with open(path, encoding="utf-8") as f:
                md = f.read()
        except Exception as e:
            logger.debug("skipping unreadable template %s: %s", fname, e)
            continue
        for m in re.finditer(r"###\s+(.+?)\n.*?- DAX:\s+`([^`]+)`", md, re.DOTALL):
            tid = m.group(1).strip()
            if tid not in out:
                out[tid] = m.group(2).strip()
    return out


def _host_columns_context(plan: SchemaPlan, host_table: str) -> list[dict]:
    for t in plan.tables:
        if t.name == host_table:
            return [{"name": c.name, "data_type": c.data_type} for c in t.columns]
    return []


def _referenced_tables_context(plan: SchemaPlan, host_table: str) -> list[dict]:
    related: set[str] = set()
    for r in plan.relationships:
        if r.from_table == host_table:
            related.add(r.to_table)
        elif r.to_table == host_table:
            related.add(r.from_table)
    out = []
    for t in plan.tables:
        if t.name in related:
            out.append(
                {
                    "name": t.name,
                    "columns": [
                        {"name": c.name, "data_type": c.data_type} for c in t.columns
                    ],
                }
            )
    return out


def _profile_samples(
    profile: dict | None, host_table: str, col_names: list[str]
) -> dict:
    if not profile:
        return {}
    info = profile.get(host_table, {})
    samples = info.get("samples", [])
    out: dict[str, list] = {c: [] for c in col_names}
    for row in samples:
        for c in col_names:
            v = row.get(c)
            if v is not None and v not in out[c] and len(out[c]) < 8:
                out[c].append(str(v))
    return out


def _grain_hints(plan: SchemaPlan, profile: dict | None) -> dict[str, str]:
    fanout_ratio = 1.01

    hints: dict[str, str] = {}
    if not profile:
        return hints
    for t in plan.tables:
        if t.kind != "fact":
            continue
        key = next((c for c in t.columns if c.is_key), None)
        tinfo = profile.get(t.name) or {}
        rows = tinfo.get("row_count") or 0
        if not key or not rows:
            continue
        cols = tinfo.get("columns") or {}
        distinct = (cols.get(key.source_column) or cols.get(key.name) or {}).get(
            "distinct"
        ) or 0
        if not distinct or rows <= distinct * fanout_ratio:
            continue
        hints[t.name] = (
            f"'{t.name}': ~{rows / distinct:.0f} rows per {key.name} "
            f"({rows:,} rows / {distinct:,} distinct) — count {key.name} with "
            f"DISTINCTCOUNT('{t.name}'[{key.name}]), never COUNTROWS."
        )
    return hints


def generate_dax_for_measure(
    spec: MeasureSpec,
    plan: SchemaPlan,
    profile: dict | None,
    templates: dict[str, str],
    project_id: int | None = None,
    max_retries: int = 2,
) -> tuple[str, str | None]:
    host_cols = _host_columns_context(plan, spec.host_table)
    referenced = _referenced_tables_context(plan, spec.host_table)
    samples = _profile_samples(profile, spec.host_table, [c["name"] for c in host_cols])
    template_dax = templates.get(spec.template_id or "", "")
    grain_note = _grain_hints(plan, profile).get(spec.host_table, "")

    user_msg = json.dumps(
        {
            "measure_name": spec.name,
            "host_table": spec.host_table,
            "intent": spec.intent,
            "template_dax": template_dax,
            "host_columns": host_cols,
            "referenced_tables": referenced[:5],
            "profile_sample_values": samples,
            "suggested_format": spec.format_string,
        },
        ensure_ascii=False,
        indent=2,
    )
    if grain_note:
        user_msg += f"\n\nGRAIN: {grain_note}"

    last_err = ""
    for attempt in range(max_retries):
        prompt = user_msg
        if last_err:
            prompt += f"\n\n## Previous attempt failed with: {last_err}\nFix and return valid JSON."

        t0 = time.time()
        raw, usage = _call_llm(MEASURE_SYSTEM_PROMPT, prompt, max_tokens=1500)
        latency_ms = int((time.time() - t0) * 1000)
        logger.info(f"Measure '{spec.name}' attempt {attempt + 1}: {latency_ms}ms")
        log_usage(project_id, "measure_generator", usage, latency_ms)

        try:
            data = json.loads(_strip_json(raw))
        except Exception as e:
            last_err = f"JSON parse failed: {e}"
            continue

        dax = (data.get("dax") or "").strip()
        if not dax:
            last_err = "Empty dax field"
            continue

        if dax == "BLANK()" or dax.startswith("//"):
            last_err = "DAX is a placeholder — write the real expression"
            continue

        dax = re.sub(r"^=\s*", "", dax)

        lint_issues = _lint_dax(dax)
        if lint_issues:
            last_err = "DAX lint failed: " + " ; ".join(lint_issues)
            continue

        ref_issues = _reference_issues(dax, plan)
        if ref_issues:
            last_err = "DAX reference check failed: " + " ; ".join(ref_issues)
            continue

        fmt = data.get("format_string")
        return dax, fmt

    raise RuntimeError(
        f"Measure '{spec.name}': could not produce DAX after {max_retries} attempts. Last error: {last_err}"
    )


BATCH_MEASURE_SYSTEM_PROMPT = """You are a DAX expert. Write DAX expressions for EVERY measure in the request. Output strict JSON only.

## Your output

Return ONLY this JSON object — no prose, no markdown fences:

{
  "<measure name 1>": {"dax": "<single-line DAX>", "format_string": "<formatString or null>"},
  "<measure name 2>": {"dax": "...", "format_string": "..."},
  ...
}

Include an entry for EVERY measure listed. If you genuinely cannot write DAX for one (missing required column, unclear intent), use:
  "<measure name>": {"dax": "", "format_string": null, "skip_reason": "<short reason>"}
— that measure will be dropped instead of broken.

## DAX rules

1. Column references use 'TableName'[ColumnName]. For tables with dots (e.g. 'DEP.factpolicytransaction'), ALWAYS single-quote the table.
2. Measure references use [Measure Name] (no table quote).
3. Filter LITERALS must match real values shown under "## Sample values" — never invent a value.
4. NEVER use a column that isn't declared on the host_table in "## Tables & columns" below.
5. Keep each `dax` value on ONE line. If logic needs VAR/RETURN, inline: `VAR x = ... RETURN ...`.
6. For boolean filters, use the comparison form: `'T'[col] = "value"`, never a raw boolean column.
7. If a measure has a template_id matching one of the "## Templates" entries, adapt that template — don't reinvent.
8. COUNTING: if a table is listed under "## Grain", its rows repeat per key — count that entity with DISTINCTCOUNT('Table'[key]) as stated there, never COUNTROWS.
9. PERCENTAGES & RATIOS (CRITICAL): In Power BI, `format_string: "0.0%"` automatically multiplies the DAX result by 100. DAX expressions for percentages/ratios/rates/shares MUST evaluate to a fraction between `0.0` and `1.0` (e.g. `0.1936` for 19.36%). NEVER write `* 100` in DAX with `"0.0%"` format. If source column is on 0-100 scale (e.g. 19.36), divide by 100: `DIVIDE(AVERAGE('Table'[Col]), 100, 0)`. NEVER use `SUM()` across percentages or rates (use `AVERAGE()` or `DIVIDE()`). All sibling percentage metrics must have `format_string: "0.0%"`.
"""


def _build_batch_prompt(
    plan: SchemaPlan, profile: dict | None, templates: dict[str, str]
) -> str:
    lines: list[str] = []

    lines.append("## Tables & columns")
    for t in plan.tables:
        col_strs = [f"{c.name}:{c.data_type}" for c in t.columns]
        lines.append(f"- {t.name} [{t.kind}]: {', '.join(col_strs)}")
    lines.append("")

    if profile:
        lines.append("## Sample values (use these literals — never invent values)")
        for t in plan.tables:
            tinfo = profile.get(t.name)
            if not tinfo:
                continue
            samples = tinfo.get("samples", [])
            per_col: dict[str, list] = {}
            for row in samples:
                for col_name, v in row.items():
                    if v is None:
                        continue
                    out = per_col.setdefault(col_name, [])
                    if v not in out and len(out) < 6:
                        out.append(v)
            if not per_col:
                continue
            sample_lines = []
            for col, vals in per_col.items():
                if not vals:
                    continue
                vals_str = ", ".join(repr(str(v)[:40]) for v in vals[:6])
                sample_lines.append(f"    {col}: [{vals_str}]")
            if sample_lines:
                lines.append(f"  {t.name}:")
                lines.extend(sample_lines)
        lines.append("")

    grain = _grain_hints(plan, profile)
    if grain:
        lines.append("## Grain (row multiplicity — affects how to COUNT)")
        lines.extend(f"- {h}" for h in grain.values())
        lines.append("")

    if templates:
        lines.append("## Templates")
        for tid, dax in templates.items():
            lines.append(f"- {tid}: `{dax}`")
        lines.append("")

    lines.append("## Measures to write DAX for")
    for i, m in enumerate(plan.measures, 1):
        lines.append(f"{i}. {m.name}")
        lines.append(f"   host_table: {m.host_table}")
        lines.append(f"   intent: {m.intent}")
        if m.template_id:
            lines.append(f"   template_id: {m.template_id}")
        if m.format_string:
            lines.append(f"   suggested_format: {m.format_string}")
        lines.append("")

    lines.append(
        f"Now return JSON with EXACTLY {len(plan.measures)} keys — one per measure listed above. JSON ONLY."
    )
    return "\n".join(lines)


def generate_dax_for_all_measures(
    plan: SchemaPlan,
    profile: dict | None,
    templates: dict[str, str],
    project_id: int | None = None,
    max_retries: int = 2,
) -> tuple[dict[str, tuple[str, str | None]], list[tuple[str, str]]]:
    if not plan.measures:
        return {}, []

    prompt = _build_batch_prompt(plan, profile, templates)
    requested_names = {m.name for m in plan.measures}

    resolved: dict[str, tuple[str, str | None]] = {}
    failed: list[tuple[str, str]] = [
        (m.name, "no batch response received") for m in plan.measures
    ]

    output_budget = max(8000, len(plan.measures) * 350)

    last_err = ""
    for attempt in range(max_retries):
        msg = prompt
        if last_err:
            msg += f"\n\n## Previous attempt failed with: {last_err}\nReturn corrected JSON."

        t0 = time.time()
        raw, usage = _call_llm(
            BATCH_MEASURE_SYSTEM_PROMPT, msg, max_tokens=output_budget
        )
        latency_ms = int((time.time() - t0) * 1000)
        logger.info(
            f"Measure batch attempt {attempt + 1}: {latency_ms}ms "
            f"for {len(plan.measures)} measures"
        )
        log_usage(project_id, "measure_generator_batch", usage, latency_ms)

        try:
            data = json.loads(_strip_json(raw))
            if not isinstance(data, dict):
                raise TypeError(f"Expected JSON object, got {type(data).__name__}")
        except Exception as e:
            last_err = f"JSON parse failed: {e}"
            continue

        resolved: dict[str, tuple[str, str | None]] = {}
        failed: list[tuple[str, str]] = []

        for name in requested_names:
            entry = data.get(name)
            if not isinstance(entry, dict):
                failed.append((name, "missing from batch response"))
                continue
            if entry.get("skip_reason"):
                failed.append((name, f"LLM skipped: {entry['skip_reason']}"))
                continue
            dax = (entry.get("dax") or "").strip()
            if not dax or dax == "BLANK()" or dax.startswith("//"):
                failed.append((name, "empty or placeholder DAX"))
                continue
            dax = re.sub(r"^=\s*", "", dax)
            lint_issues = _lint_dax(dax)
            if lint_issues:
                failed.append((name, "lint: " + " ; ".join(lint_issues)))
                continue
            ref_issues = _reference_issues(dax, plan)
            if ref_issues:
                failed.append((name, "refs: " + " ; ".join(ref_issues)))
                continue
            fmt = entry.get("format_string")
            resolved[name] = (dax, fmt)

        if len(resolved) >= 0.7 * len(plan.measures) or attempt == max_retries - 1:
            return resolved, failed

        last_err = (
            f"Batch returned valid DAX for only {len(resolved)}/{len(plan.measures)} measures. "
            f"Missing/invalid: {[n for n, _ in failed][:6]}..."
        )

    return resolved, failed


def populate_measures(
    plan: SchemaPlan,
    profile: dict | None = None,
    project_id: int | None = None,
    progress_callback=None,
) -> tuple[SchemaPlan, list[tuple[str, str]]]:
    templates = _load_templates()

    if progress_callback:
        progress_callback(
            0,
            len(plan.measures),
            "[batched] writing DAX for all measures in one call...",
        )

    resolved, _batch_failures = generate_dax_for_all_measures(
        plan, profile, templates, project_id=project_id
    )

    if progress_callback:
        progress_callback(
            len(resolved),
            len(plan.measures),
            f"batch done — {len(resolved)}/{len(plan.measures)} resolved",
        )

    surviving: list[MeasureSpec] = []
    failures: list[tuple[str, str]] = []

    for i, m in enumerate(plan.measures):
        if m.name in resolved:
            dax, fmt = resolved[m.name]
            m.dax = dax
            if fmt:
                m.format_string = fmt
            surviving.append(m)
            continue

        if progress_callback:
            progress_callback(i + 1, len(plan.measures), f"fallback: {m.name}")
        try:
            dax, fmt = generate_dax_for_measure(
                m, plan, profile, templates, project_id=project_id
            )
            m.dax = dax
            if fmt:
                m.format_string = fmt
            surviving.append(m)
        except Exception as e:
            logger.error(
                f"Measure '{m.name}' failed; shipping BLANK() placeholder: {e}"
            )
            m.dax = "BLANK()"
            surviving.append(m)
            failures.append((m.name, str(e)))

    plan.measures = surviving
    return plan, failures
