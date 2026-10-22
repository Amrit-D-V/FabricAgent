from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from util.json_utils import strip_json_fences as _strip_json_fences

from ._llm import stream_collect
from .plan import (
    ColumnSpec,
    MeasureSpec,
    SchemaPlan,
    TableSpec,
    plan_to_dict,
)
from .repair import _sql_to_tmdl

logger = logging.getLogger(__name__)


CRITIC_SYSTEM_PROMPT = """You are a Power BI semantic-model critic. Given a draft plan, business requirements, and (optionally) a reference model to imitate, your job is ONLY to find gaps and suggest deltas. Never re-generate the whole plan.

## Inputs you receive
- `current_plan`: the draft SchemaPlan JSON (tables, relationships, measures).
- `requirements`: free-text + (optional) explicit KPI list.
- `reference_model`: (optional) summary of an existing PBIP whose patterns should be imitated where they fit.

## Output

Return ONLY this JSON object — no prose, no markdown fences:

{
  "diagnosis": "1-3 short sentences naming the biggest gaps you see",
  "add_measures": [
    {
      "name": "string — measure display name",
      "host_table": "string — must be one of the plan's tables (do NOT invent a table here)",
      "intent": "string — what this measure means in business terms (drives Phase 6 DAX gen)",
      "template_id": "string or null — match a name from the template library if known",
      "format_string": "string or null — e.g. '#,##0' or '0.0%'",
      "display_folder": "string or null",
      "reason": "string — why this measure is needed (which requirement it satisfies)"
    }
  ],
  "drop_measures": [
    {
      "name": "string — measure name to remove",
      "reason": "string — why (duplicate, can't be computed with available columns, etc.)"
    }
  ],
  "add_tables": [
    {
      "name": "full source table name (must exist in the warehouse schema)",
      "kind": "fact" | "dim" | "calculated",
      "reason": "string"
    }
  ],
  "rename_measures": [
    { "from": "string — current name", "to": "string — new name", "reason": "string" }
  ],
  "notes": ["short string findings the user might want to read"]
}

## Rules

1. **Never invent tables that aren't in the warehouse schema**. If a requirement needs a missing table, mention it in `notes` instead of `add_tables`.
2. **Every `add_measures` host_table MUST be in the current plan's tables[]**. Validate before emitting.
3. **Only suggest measures the requirements explicitly need.** Don't pad with extra "nice to have" measures.
4. **Diagnose, don't redesign.** If the plan's relationship topology is correct, don't touch it.
5. **Keep output COMPACT** — `reason` strings ≤ 80 chars each. The critic's output is post-processed deterministically; verbose text just wastes tokens.
6. **No prose outside JSON. No markdown fences. The first character of your output MUST be `{`.**

## Specific gap checks the critic MUST perform

7. **VIEW INCLUSION CHECK** — Walk the `current_plan`'s `tables[]` and count how
   many table names start with `vw_` (case-insensitive). Cross-reference with
   the warehouse schema in the requirements/reference context: count
   `CREATE VIEW` declarations there. If the schema has VIEW objects that
   are NOT in `tables[]`, add them via `add_tables` with kind=`fact`
   (unless < 5 columns and mostly text → `dim`) and a reason naming the
   specific view. View-tables are the #1 silently-dropped category — be
   aggressive about catching this gap.

8. **KPI COVERAGE CHECK** — If a KPI requirements block is present, walk
   every spec KPI line. For each one not already covered by a measure in
   `current_plan.measures` (Jaccard token match >= 0.5 on lowercase),
   emit an `add_measures` entry naming the host_table that has the
   columns the formula needs. If the formula references columns the model
   doesn't have, list the KPI in `notes` as `"BLOCKED: <name> — needs
   <source>"` instead.

9. **DOMAIN-SHAPE SANITY CHECK** — If the current plan classified the
   schema as hub-and-spoke but `tables[]` shows 5+ apparent root facts
   without a single shared hub-key column, add a note flagging the
   misclassification: `"Domain may be multi-root-snowflake — re-examine."`

## When the critic should be silent

If the current plan already covers every requirement and matches the reference, emit:
  {"diagnosis": "Plan looks complete", "add_measures": [], "drop_measures": [], "add_tables": [], "rename_measures": [], "notes": []}

That's a valid response. Don't fabricate gaps to appear useful.
"""


@dataclass
class CriticPatch:

    diagnosis: str = ""
    add_measures: list[dict] = field(default_factory=list)
    drop_measures: list[dict] = field(default_factory=list)
    add_tables: list[dict] = field(default_factory=list)
    rename_measures: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return (
            not self.add_measures
            and not self.drop_measures
            and not self.add_tables
            and not self.rename_measures
        )

    def summary(self) -> str:
        bits = []
        if self.add_measures:
            bits.append(f"+{len(self.add_measures)} measures")
        if self.drop_measures:
            bits.append(f"-{len(self.drop_measures)} measures")
        if self.add_tables:
            bits.append(f"+{len(self.add_tables)} tables")
        if self.rename_measures:
            bits.append(f"~{len(self.rename_measures)} renames")
        return ", ".join(bits) if bits else "no changes"


def critique_plan(
    plan: SchemaPlan,
    requirements: str,
    kpi_requirements_block: str = "",
    reference_model_block: str = "",
    progress_callback: Callable[[str], None] | None = None,
) -> CriticPatch:
    pd = plan_to_dict(plan)
    for t in pd.get("tables", []):
        for c in t.get("columns", []):
            c.pop("description", None)
            c.pop("synonyms", None)
        t.pop("description", None)
        t.pop("synonyms", None)
    for m in pd.get("measures", []):
        m.pop("synonyms", None)
        m.pop("dax", None)
    plan_compact = json.dumps(pd, indent=2)

    parts = [
        "## Current draft plan",
        "```json",
        plan_compact,
        "```",
        "",
        "## Requirements",
        requirements or "(no free-text requirements provided)",
    ]
    if kpi_requirements_block:
        parts += [
            "",
            "## Explicit KPI requirements (parsed from CSV)",
            kpi_requirements_block,
        ]
    if reference_model_block:
        parts += ["", "## Reference model to imitate", reference_model_block]
    parts += [
        "",
        "Critique the plan now. Output ONLY the JSON patch — no prose, no fences.",
    ]
    user_msg = "\n".join(parts)

    t0 = time.time()
    if progress_callback:
        progress_callback("Phase 3.7 (critic): reviewing plan against requirements...")

    raw, usage = stream_collect(
        CRITIC_SYSTEM_PROMPT,
        user_msg,
        agent="big_task",
        max_tokens=8000,
    )
    latency_ms = int((time.time() - t0) * 1000)

    try:
        data = json.loads(_strip_json_fences(raw))
    except Exception as e:
        logger.warning(
            f"Critic JSON parse failed in {latency_ms}ms: {e}. "
            f"Output ends with: ...{raw[-200:]!r}"
        )
        return CriticPatch(diagnosis="(critic parse failed — skipping)")

    patch = CriticPatch(
        diagnosis=data.get("diagnosis", ""),
        add_measures=list(data.get("add_measures", [])) or [],
        drop_measures=list(data.get("drop_measures", [])) or [],
        add_tables=list(data.get("add_tables", [])) or [],
        rename_measures=list(data.get("rename_measures", [])) or [],
        notes=list(data.get("notes", [])) or [],
    )
    logger.info(f"Critic in {latency_ms}ms: {patch.summary()}")
    if usage:
        logger.info(
            f"Critic tokens: in={usage.input_tokens}, out={usage.output_tokens}"
        )
    return patch


def apply_critic_patch(
    plan: SchemaPlan,
    patch: CriticPatch,
    schema_tables: dict[str, list[tuple[str, str]]],
) -> tuple[SchemaPlan, list[str]]:
    actions: list[str] = []
    plan_table_names = {t.name for t in plan.tables}

    for entry in patch.add_tables:
        name = entry.get("name", "").strip()
        kind = entry.get("kind", "dim").strip()
        if not name:
            continue
        if name in plan_table_names:
            continue
        if name not in schema_tables:
            actions.append(
                f"SKIP add_table '{name}': not in source schema ({entry.get('reason', '')})"
            )
            continue
        if kind not in {"fact", "dim", "calculated"}:
            kind = "dim"
        cols = [
            ColumnSpec(name=c[0], source_column=c[0], data_type=_sql_to_tmdl(c[1]))
            for c in schema_tables[name]
        ]
        for c in cols:
            if c.name in {
                "sourceSystem",
                "loadTimestamp",
                "pipelineRunId",
                "recordSource",
                "recordVersion",
            }:
                c.is_hidden = True
        if "." in name:
            schema, src = name.split(".", 1)
        else:
            schema, src = None, name
        plan.tables.append(
            TableSpec(
                name=name,
                kind=kind,
                schema=schema,
                source_table=src,
                columns=cols,
            )
        )
        plan_table_names.add(name)
        actions.append(f"+table '{name}' (kind={kind}): {entry.get('reason', '')}")

    existing_measure_names = {m.name for m in plan.measures}
    for entry in patch.add_measures:
        name = (entry.get("name") or "").strip()
        host = (entry.get("host_table") or "").strip()
        intent = (entry.get("intent") or "").strip()
        if not (name and host and intent):
            continue
        if name in existing_measure_names:
            actions.append(f"SKIP add_measure '{name}': already present")
            continue
        if host not in plan_table_names:
            actions.append(
                f"SKIP add_measure '{name}': host_table '{host}' not in plan"
            )
            continue
        plan.measures.append(
            MeasureSpec(
                name=name,
                host_table=host,
                intent=intent,
                template_id=entry.get("template_id"),
                format_string=entry.get("format_string"),
                display_folder=entry.get("display_folder"),
            )
        )
        existing_measure_names.add(name)
        actions.append(f"+measure '{name}' on {host}: {entry.get('reason', '')}")

    if patch.drop_measures:
        names_to_drop = {(e.get("name") or "").strip() for e in patch.drop_measures}
        before = len(plan.measures)
        survivors = []
        for m in plan.measures:
            if m.name in names_to_drop:
                reason = next(
                    (
                        e.get("reason", "")
                        for e in patch.drop_measures
                        if e.get("name") == m.name
                    ),
                    "",
                )
                actions.append(f"-measure '{m.name}': {reason}")
            else:
                survivors.append(m)
        plan.measures = survivors
        if len(plan.measures) != before:
            actions.append(
                f"Dropped {before - len(plan.measures)} measure(s) per critic"
            )

    for entry in patch.rename_measures:
        old = (entry.get("from") or "").strip()
        new = (entry.get("to") or "").strip()
        if not (old and new) or old == new:
            continue
        target = next((m for m in plan.measures if m.name == old), None)
        if target is None:
            actions.append(f"SKIP rename '{old}' -> '{new}': not in plan")
            continue
        if new in existing_measure_names:
            actions.append(f"SKIP rename '{old}' -> '{new}': name collision")
            continue
        target.name = new
        actions.append(f"~rename '{old}' -> '{new}': {entry.get('reason', '')}")

    return plan, actions
