from __future__ import annotations

import json
import logging
import time
from copy import deepcopy

from llm.llm_client import UsageInfo

from ._llm import log_usage, stream_collect
from .plan import dict_to_plan
from .validate import validate_plan

logger = logging.getLogger(__name__)


REFINE_SYSTEM_PROMPT = """You are a Power BI semantic-model architect editing an EXISTING plan via natural-language requests. You produce a structured JSON PATCH — never TMDL. The patch is applied to the plan deterministically, then validated, then re-rendered to TMDL.

## Output format

Return ONLY a single JSON object — no prose, no markdown:

{
  "operations": [
    { "kind": "...", ...op-specific fields... },
    ...
  ]
}

If the user's request can't be satisfied (asks for something not present, ambiguous, or unsafe), return:
  { "operations": [], "reason": "<one short sentence explaining what's missing>" }

## Operations supported

### add_measure
{
  "kind": "add_measure",
  "host_table": "<must exist in plan.tables>",
  "name": "<unique, NOT equal case-insensitively to any column on host_table>",
  "intent": "<one-sentence business description; drives DAX generation>",
  "template_id": "<optional — e.g. 'Year-over-Year delta', 'YTD (Year-to-Date)', 'Sum'>",
  "format_string": "<optional — '#,##0' or '0.0%' etc>"
}

### drop_measure
{ "kind": "drop_measure", "name": "<measure name to remove>" }

### rename_measure
{ "kind": "rename_measure", "old_name": "...", "new_name": "..." }

### update_measure
{
  "kind": "update_measure",
  "name": "<measure to modify>",
  "new_intent": "<optional new intent; if provided, DAX will be regenerated>",
  "new_format_string": "<optional>",
  "new_template_id": "<optional>"
}

### add_relationship
{
  "kind": "add_relationship",
  "from_table": "...", "from_column": "...",
  "to_table": "...", "to_column": "...",
  "is_active": true
}

### drop_relationship
{
  "kind": "drop_relationship",
  "from_table": "...", "from_column": "...",
  "to_table": "...", "to_column": "..."
}

### add_column
{
  "kind": "add_column",
  "table": "...",
  "column": {
    "name": "...",
    "source_column": "...",
    "data_type": "int64|double|string|dateTime|boolean",
    "is_hidden": false,
    "is_key": false,
    "summarize_by": "none|sum|count|null"
  }
}

### drop_column
{ "kind": "drop_column", "table": "...", "name": "..." }

### update_column
{
  "kind": "update_column",
  "table": "...",
  "name": "...",
  "is_hidden": true,
  "is_key": false,
  "summarize_by": "none",
  "display_folder": "<optional>"
}

### rename_column
{ "kind": "rename_column", "table": "...", "old_name": "...", "new_name": "..." }

### rename_column_everywhere
{ "kind": "rename_column_everywhere", "old_name": "...", "new_name": "..." }

### add_table
{
  "kind": "add_table",
  "name": "<full name e.g. 'DEP.factsubmission'>",
  "schema": "<schema prefix>",
  "source_table": "<bare table name>",
  "table_kind": "fact|dim",
  "columns": [ { ...column shape as above... }, ... ]
}

### drop_table
{ "kind": "drop_table", "name": "..." }
(Also drops every relationship and measure that referenced the table.)

## Rules

1. **Read the plan you receive carefully** — table names, column names, measure names. NEVER invent a column that isn't declared on a plan table.
2. **Case-insensitive collision rule**: measure name MUST NOT equal any column name on the same host table when lower-cased. If user says "add a Taxes measure", and `column taxes` exists, name the measure "Total Taxes" instead and explain in `reason`.
3. **Star schema**: relationships go fact → dim. Never emit `add_relationship` with both sides being dims.
4. **Don't touch what you weren't asked**. Minimal patches. Single-purpose ops.
5. **When unsure**, return `operations: []` with a clear `reason` rather than guessing.

## Examples

User: "Add a YoY measure for Net Premium Written"
→ {"operations": [{"kind": "add_measure", "host_table": "DEP.factpolicytransaction", "name": "NPW YoY", "intent": "Net Premium Written change vs same period last year", "template_id": "Year-over-Year delta", "format_string": "#,##0"}]}

User: "Drop the adjuster dim"
→ {"operations": [{"kind": "drop_table", "name": "dbo.golddimadjuster"}]}

User: "Hide every column ending in Key on dbo.golddimagent"
→ {"operations": [{"kind": "update_column", "table": "dbo.golddimagent", "name": "agentKey", "is_hidden": true}]}  (only emit one op per matching column you can see in the plan)

User: "Rename Net Written Premium to NWP"
→ {"operations": [{"kind": "rename_measure", "old_name": "Net Written Premium", "new_name": "NWP"}]}

User: "Add a measure called Loss Ratio"
→ {"operations": [{"kind": "add_measure", "host_table": "DEC.factclaim", "name": "Loss Ratio", "intent": "Total Losses Incurred divided by Total Earned Premium across all dim contexts", "template_id": "Ratio (safe divide)", "format_string": "0.0%"}]}
"""


def _find_table(plan_dict: dict, name: str) -> dict | None:
    for t in plan_dict.get("tables", []):
        if t["name"] == name:
            return t
    return None


def _has_column_ci(table: dict, col_name: str) -> str | None:
    target = col_name.lower()
    for c in table.get("columns", []):
        if c["name"].lower() == target:
            return c["name"]
    return None


def _apply_operation(plan_dict: dict, op: dict) -> tuple[bool, str]:
    kind = op.get("kind")

    if kind == "add_measure":
        name = (op.get("name") or "").strip()
        if not name:
            return False, "add_measure: missing 'name'"
        host = _find_table(plan_dict, op.get("host_table", ""))
        if host is None:
            return (
                False,
                f"add_measure: host_table '{op.get('host_table')}' not in plan",
            )
        host_table = host["name"]
        if _has_column_ci(host, name):
            return False, (
                f"add_measure: name '{name}' collides case-insensitively with a "
                f"column on '{host_table}'. Suggest 'Total {name}'."
            )
        existing_ci = {m["name"].lower() for m in plan_dict.get("measures", [])}
        if name.lower() in existing_ci:
            return (
                False,
                f"add_measure: measure '{name}' already exists (case-insensitive)",
            )
        plan_dict.setdefault("measures", []).append(
            {
                "name": name,
                "host_table": host_table,
                "intent": op.get("intent", ""),
                "template_id": op.get("template_id"),
                "format_string": op.get("format_string"),
                "display_folder": op.get("display_folder"),
                "dax": "",
            }
        )
        return True, f"+ measure '{name}' on {host_table}"

    if kind == "drop_measure":
        name = op.get("name", "")
        before = len(plan_dict.get("measures", []))
        plan_dict["measures"] = [
            m for m in plan_dict.get("measures", []) if m["name"] != name
        ]
        dropped = before - len(plan_dict["measures"])
        if not dropped:
            return False, f"drop_measure: measure '{name}' not in plan"
        return True, f"- measure '{name}'"

    if kind == "rename_measure":
        old, new = op.get("old_name", ""), op.get("new_name", "")
        for m in plan_dict.get("measures", []):
            if m["name"] == old:
                m["name"] = new
                return True, f"~ measure '{old}' → '{new}'"
        return False, f"rename_measure: '{old}' not in plan"

    if kind == "update_measure":
        name = op.get("name", "")
        for m in plan_dict.get("measures", []):
            if m["name"] != name:
                continue
            changes: list[str] = []
            if "new_intent" in op:
                m["intent"] = op["new_intent"]
                m["dax"] = ""
                changes.append("intent")
            if "new_format_string" in op:
                m["format_string"] = op["new_format_string"]
                changes.append("format")
            if "new_template_id" in op:
                m["template_id"] = op["new_template_id"]
                m["dax"] = ""
                changes.append("template")
            if not changes:
                return False, f"update_measure: no changes specified for '{name}'"
            return True, f"~ measure '{name}' ({', '.join(changes)})"
        return False, f"update_measure: '{name}' not in plan"

    if kind == "add_relationship":
        ft = op.get("from_table")
        fc = op.get("from_column")
        tt = op.get("to_table")
        tc = op.get("to_column")
        if not ft or not fc or not tt or not tc:
            return (
                False,
                "add_relationship: missing from_table/from_column/to_table/to_column",
            )
        if _find_table(plan_dict, str(ft)) is None:
            return False, f"add_relationship: from_table '{ft}' not in plan"
        if _find_table(plan_dict, str(tt)) is None:
            return False, f"add_relationship: to_table '{tt}' not in plan"
        plan_dict.setdefault("relationships", []).append(
            {
                "from_table": ft,
                "from_column": fc,
                "to_table": tt,
                "to_column": tc,
                "is_active": op.get("is_active", True),
                "cross_filter": op.get("cross_filter", "singleDirection"),
                "rationale": op.get("rationale", "added via chat refinement"),
            }
        )
        return True, f"+ relationship {ft}.{fc} → {tt}.{tc}"

    if kind == "drop_relationship":
        ft = op.get("from_table")
        fc = op.get("from_column")
        tt = op.get("to_table")
        tc = op.get("to_column")
        if not ft or not fc or not tt or not tc:
            return (
                False,
                "drop_relationship: missing from_table/from_column/to_table/to_column",
            )
        before = len(plan_dict.get("relationships", []))
        plan_dict["relationships"] = [
            r
            for r in plan_dict.get("relationships", [])
            if not (
                r["from_table"] == ft
                and r["from_column"] == fc
                and r["to_table"] == tt
                and r["to_column"] == tc
            )
        ]
        dropped = before - len(plan_dict["relationships"])
        if not dropped:
            return False, f"drop_relationship: no rel matching {ft}.{fc} → {tt}.{tc}"
        return True, f"- relationship {ft}.{fc} → {tt}.{tc}"

    if kind == "add_column":
        table = _find_table(plan_dict, op.get("table", ""))
        if table is None:
            return False, f"add_column: table '{op.get('table')}' not in plan"
        col = op.get("column", {})
        col_name = (col.get("name") or "").strip()
        if not col_name:
            return False, "add_column: missing column 'name'"
        if _has_column_ci(table, col_name):
            return (
                False,
                f"add_column: '{col_name}' already exists on '{table['name']}'",
            )
        table.setdefault("columns", []).append(
            {
                "name": col_name,
                "source_column": col.get("source_column", col_name),
                "data_type": col.get("data_type", "string"),
                "is_hidden": col.get("is_hidden", False),
                "is_key": col.get("is_key", False),
                "summarize_by": col.get("summarize_by"),
                "display_folder": col.get("display_folder"),
                "description": col.get("description"),
                "dax_expression": col.get("dax_expression"),
            }
        )
        return True, f"+ column '{col_name}' on {table['name']}"

    if kind == "drop_column":
        table = _find_table(plan_dict, op.get("table", ""))
        if table is None:
            return False, f"drop_column: table '{op.get('table')}' not in plan"
        name = op.get("name", "")
        cols = table.get("columns", [])
        before = len(cols)
        table["columns"] = [c for c in cols if c.get("name") != name]
        if before == len(table["columns"]):
            return False, f"drop_column: column '{name}' not on '{table['name']}'"
        return True, f"- column '{name}' from {table['name']}"

    if kind == "update_column":
        table = _find_table(plan_dict, op.get("table", ""))
        if table is None:
            return False, f"update_column: table '{op.get('table')}' not in plan"
        for c in table.get("columns", []):
            if c["name"] != op.get("name", ""):
                continue
            changes = []
            for field in (
                "is_hidden",
                "is_key",
                "summarize_by",
                "display_folder",
                "data_type",
                "description",
                "dax_expression",
            ):
                if field in op:
                    c[field] = op[field]
                    changes.append(field)
            if not changes:
                return (
                    False,
                    f"update_column: no changes specified for '{op.get('name')}'",
                )
            return (
                True,
                f"~ column '{op['name']}' on {table['name']} ({', '.join(changes)})",
            )
        return (
            False,
            f"update_column: column '{op.get('name')}' not found on '{table['name']}'",
        )

    if kind == "rename_column":
        table = _find_table(plan_dict, op.get("table", ""))
        if table is None:
            return False, f"rename_column: table '{op.get('table')}' not in plan"
        old, new = op.get("old_name", ""), op.get("new_name", "")
        for c in table.get("columns", []):
            if c["name"] == old:
                c["name"] = new
                for r in plan_dict.get("relationships", []):
                    if r["from_table"] == table["name"] and r["from_column"] == old:
                        r["from_column"] = new
                    if r["to_table"] == table["name"] and r["to_column"] == old:
                        r["to_column"] = new
                return True, f"~ column '{old}' → '{new}' on {table['name']}"
        return False, f"rename_column: '{old}' not found on '{table['name']}'"

    if kind == "rename_column_everywhere":
        old, new = op.get("old_name", ""), op.get("new_name", "")
        n_cols = 0
        for t in plan_dict.get("tables", []):
            for c in t.get("columns", []):
                if c["name"] == old:
                    c["name"] = new
                    n_cols += 1
        n_rels = 0
        for r in plan_dict.get("relationships", []):
            if r["from_column"] == old:
                r["from_column"] = new
                n_rels += 1
            if r["to_column"] == old:
                r["to_column"] = new
                n_rels += 1
        if not n_cols and not n_rels:
            return False, f"rename_column_everywhere: '{old}' not found anywhere"
        return (
            True,
            f"~ '{old}' → '{new}' (renamed {n_cols} columns, {n_rels} rel refs)",
        )

    if kind == "add_table":
        name = op.get("name", "")
        if _find_table(plan_dict, name) is not None:
            return False, f"add_table: '{name}' already in plan"
        cols = [
            {
                "name": c["name"],
                "source_column": c.get("source_column", c["name"]),
                "data_type": c.get("data_type", "string"),
                "is_hidden": c.get("is_hidden", False),
                "is_key": c.get("is_key", False),
                "summarize_by": c.get("summarize_by"),
                "display_folder": c.get("display_folder"),
                "description": c.get("description"),
                "dax_expression": c.get("dax_expression"),
            }
            for c in op.get("columns", [])
        ]
        plan_dict.setdefault("tables", []).append(
            {
                "name": name,
                "kind": op.get("table_kind", "dim"),
                "schema": op.get("schema"),
                "source_table": op.get("source_table"),
                "is_hidden": op.get("is_hidden", False),
                "description": op.get("description"),
                "columns": cols,
            }
        )
        return (
            True,
            f"+ table '{name}' ({op.get('table_kind', 'dim')}, {len(cols)} columns)",
        )

    if kind == "drop_table":
        name = op.get("name", "")
        if _find_table(plan_dict, name) is None:
            return False, f"drop_table: '{name}' not in plan"
        plan_dict["tables"] = [t for t in plan_dict["tables"] if t["name"] != name]
        rel_before = len(plan_dict.get("relationships", []))
        plan_dict["relationships"] = [
            r
            for r in plan_dict.get("relationships", [])
            if r["from_table"] != name and r["to_table"] != name
        ]
        rel_dropped = rel_before - len(plan_dict["relationships"])
        mea_before = len(plan_dict.get("measures", []))
        plan_dict["measures"] = [
            m for m in plan_dict.get("measures", []) if m["host_table"] != name
        ]
        mea_dropped = mea_before - len(plan_dict["measures"])
        return True, (
            f"- table '{name}' (cascade-dropped {rel_dropped} relationship(s), "
            f"{mea_dropped} measure(s))"
        )

    return False, f"unknown operation: {kind!r}"


def apply_plan_patch(plan_dict: dict, patch: dict) -> tuple[dict, list[str], list[str]]:
    new_plan = deepcopy(plan_dict)
    action_log: list[str] = []
    errors: list[str] = []
    for i, op in enumerate(patch.get("operations", [])):
        kind = op.get("kind", "?") if isinstance(op, dict) else "?"
        try:
            ok, msg = _apply_operation(new_plan, op)
        except Exception as e:
            errors.append(f"op[{i}] ({kind}): malformed operation skipped — {e}")
            continue
        if ok:
            action_log.append(msg)
        else:
            errors.append(f"op[{i}] ({kind}): {msg}")
    return new_plan, action_log, errors


from util.json_utils import strip_json_fences as _strip_json_fences


def _compact_plan_for_prompt(plan_dict: dict) -> str:
    lines = ["## Current plan"]
    for t in plan_dict.get("tables", []):
        cols = ", ".join(f"{c['name']}:{c['data_type']}" for c in t.get("columns", []))
        lines.append(f"- table {t['name']} [{t.get('kind', 'dim')}]: {cols}")
    lines.append("")
    lines.append("## Measures")
    for m in plan_dict.get("measures", []):
        lines.append(
            f"- {m['name']} (host={m['host_table']}, intent={m.get('intent', '')[:80]})"
        )
    lines.append("")
    lines.append("## Active relationships")
    for r in plan_dict.get("relationships", []):
        lines.append(
            f"- {r['from_table']}.{r['from_column']} → {r['to_table']}.{r['to_column']}"
        )
    return "\n".join(lines)


def generate_refine_patch(
    plan_dict: dict,
    instruction: str,
    chat_history: list[dict] | None = None,
    project_id: int | None = None,
    max_retries: int = 2,
    progress_callback=None,
) -> tuple[dict, UsageInfo | None]:
    plan_summary = _compact_plan_for_prompt(plan_dict)
    history_block = ""
    if chat_history:
        history_lines = ["## Conversation so far (most recent last)"]
        for msg in chat_history[-6:]:
            role = msg.get("role", "user")
            content = msg.get("content", "")[:300]
            history_lines.append(f"- {role}: {content}")
        history_block = "\n".join(history_lines) + "\n\n"

    user_msg = (
        f"{plan_summary}\n\n"
        f"{history_block}"
        f"## User instruction\n{instruction.strip()}\n\n"
        "Produce the JSON patch now. ONLY JSON."
    )

    last_err = ""
    for attempt in range(max_retries):
        msg = user_msg
        if last_err:
            msg += f"\n\n## Previous attempt failed: {last_err}\nReturn corrected JSON."

        t0 = time.time()
        if progress_callback:
            progress_callback(
                f"LLM call (attempt {attempt + 1}/{max_retries}) — streaming patch JSON..."
            )
        raw, usage = stream_collect(
            REFINE_SYSTEM_PROMPT,
            msg,
            agent="small_task",
            max_tokens=4000,
            temperature=0,
            progress_callback=progress_callback,
            progress_label="patch JSON",
            tick_every=200,
        )
        latency_ms = int((time.time() - t0) * 1000)
        if progress_callback:
            progress_callback(
                f"LLM finished in {latency_ms / 1000:.1f}s — parsing patch JSON..."
            )
        log_usage(project_id, "plan_refine", usage, latency_ms)

        try:
            cleaned = _strip_json_fences(raw)
            patch = json.loads(cleaned)
        except Exception as e:
            last_err = f"JSON parse failed: {e}"
            continue
        if not isinstance(patch, dict):
            last_err = f"expected JSON object, got {type(patch).__name__}"
            continue
        if "operations" not in patch:
            last_err = "missing 'operations' key"
            continue
        return patch, usage

    raise RuntimeError(
        f"Refine patch generation exhausted {max_retries} retries. Last error: {last_err}"
    )


def validate_patched_plan(
    new_plan_dict: dict,
    schema_tables: dict[str, list[tuple[str, str]]],
    profile: dict | None = None,
) -> tuple[list, list]:
    try:
        plan = dict_to_plan(new_plan_dict)
    except Exception as e:
        from .plan import PlanError

        return [
            PlanError(
                severity="error",
                where="plan",
                message=f"plan reconstruction failed: {e}",
            )
        ], []
    issues = validate_plan(plan, schema_tables, profile=profile)
    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warning"]
    return errors, warnings


def format_action_log(action_log: list[str]) -> str:
    if not action_log:
        return "(no changes)"
    return "\n".join(action_log)
