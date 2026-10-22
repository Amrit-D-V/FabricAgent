import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from util.json_utils import strip_json_fences as _strip_json_fences

from ._llm import stream_collect
from .business_validate import validate_measures_business
from .kpi_backfill import _KPI_LINE
from .plan import SchemaPlan, plan_to_dict, resolve_source_key

logger = logging.getLogger(__name__)

SqlProbe = Callable[[str], tuple[list, str | None]]

_WORD = re.compile(r"[a-z0-9]+")
_QUOTED_REF = re.compile(r"'([^']+)'\s*\[([^\]]+)\]")
_BARE_REF = re.compile(r"(?<![\w'\]])([A-Za-z_]\w*)\s*\[([^\]]+)\]")
_BLOCKED_LINE = re.compile(r"BLOCKED:\s*\[?(\d+)\]?\s*([^—|\n]+?)(?:\s*—|$)")

MAX_ROUNDS_HARD_CAP = 6


@dataclass
class VerifyIssue:

    severity: str
    category: str
    target: str
    message: str

    def key(self) -> tuple:
        return (self.category, self.target.lower(), self.message[:120])

    def __str__(self) -> str:
        return (
            f"[{self.severity.upper()}] {self.target} "
            f"({self.category}): {self.message}"
        )


def _tokens(s: str) -> set:
    return set(_WORD.findall((s or "").lower()))


def _measure_column_refs(dax: str | None) -> set[tuple[str, str]]:
    refs: set[tuple[str, str]] = set()
    for tbl, col in _QUOTED_REF.findall(dax or ""):
        refs.add((tbl.strip(), col.strip()))
    for tbl, col in _BARE_REF.findall(dax or ""):
        refs.add((tbl.strip(), col.strip()))
    return refs


def collect_placeholder_issues(plan: SchemaPlan) -> list[VerifyIssue]:
    out: list[VerifyIssue] = []
    for m in plan.measures:
        if (m.dax or "").strip().upper() in ("", "BLANK()"):
            out.append(
                VerifyIssue(
                    severity="error",
                    category="placeholder",
                    target=m.name,
                    message=(
                        f"DAX is a BLANK() placeholder - it was never successfully "
                        f"generated. Intent: {(m.intent or '(none)')[:160]}"
                    ),
                )
            )
    return out


def collect_business_issues(
    plan: SchemaPlan, profile: dict | None
) -> list[VerifyIssue]:
    out: list[VerifyIssue] = []
    try:
        for i in validate_measures_business(plan, profile=profile):
            if i.severity != "error":
                continue
            out.append(
                VerifyIssue(
                    severity="error",
                    category=f"business:{i.category}",
                    target=i.measure,
                    message=i.message,
                )
            )
    except Exception as e:
        logger.warning("business validation failed inside verify loop: %s", e)
    return out


def collect_coverage_issues(
    plan: SchemaPlan, kpi_requirements_block: str
) -> tuple[list[VerifyIssue], str]:
    if not kpi_requirements_block:
        return [], ""

    spec_names: list[str] = []
    seen: set[str] = set()
    for ln in kpi_requirements_block.splitlines():
        m = _KPI_LINE.match(ln.strip())
        if not m:
            continue
        full = f"{m.group(1).strip()} {m.group(2).strip()}"
        if full.lower() not in seen:
            seen.add(full.lower())
            spec_names.append(full)

    blocked_names: set[str] = set()
    for note in plan.notes or []:
        m = _BLOCKED_LINE.search(note)
        if m:
            blocked_names.add(m.group(2).strip().lower())

    matched = 0
    blocked = 0
    unmatched: list[str] = []
    for name in spec_names:
        a = _tokens(name)
        if not a:
            continue
        if any(a == _tokens(bn) for bn in blocked_names):
            blocked += 1
            continue
        best = 0.0
        for m in plan.measures:
            b = _tokens(m.name)
            if not b:
                continue
            j = len(a & b) / len(a | b)
            best = max(best, j)
        if best >= 0.5:
            matched += 1
        else:
            unmatched.append(name)

    total = len(spec_names)
    achievable = max(total - blocked, 1)
    pct = matched / achievable
    summary = (
        f"matched={matched} blocked={blocked} missing={len(unmatched)} "
        f"of {total} spec KPIs - achievable coverage "
        f"{matched}/{achievable} ({pct:.0%})"
    )

    issues: list[VerifyIssue] = []
    if pct < 0.6:
        for name in unmatched:
            issues.append(
                VerifyIssue(
                    severity="error",
                    category="coverage",
                    target=name,
                    message=(
                        "KPI required by the attached spec has no matching measure "
                        "in the plan. Add it, or record in notes why the source "
                        "data cannot support it."
                    ),
                )
            )
    return issues, summary


def collect_sql_probe_issues(
    plan: SchemaPlan,
    schema_tables: dict[str, list[tuple[str, str]]],
    sql_probe: SqlProbe,
    progress_callback: Callable[[str], None] | None = None,
) -> list[VerifyIssue]:
    out: list[VerifyIssue] = []

    by_name = {t.name: t for t in plan.tables}
    col_lookup: dict[str, dict[str, str]] = {}
    for t in plan.tables:
        col_lookup[t.name] = {
            c.name.lower(): (c.source_column or c.name) for c in t.columns
        }

    wanted: dict[tuple[str, str], set[str]] = {}
    for m in plan.measures:
        if not (m.dax or "").strip():
            continue
        for tbl, col in _measure_column_refs(m.dax):
            t = by_name.get(tbl)
            if t is None or t.kind == "calculated":
                continue
            src_col = col_lookup.get(tbl, {}).get(col.lower())
            if not src_col:
                out.append(
                    VerifyIssue(
                        severity="error",
                        category="live_sql",
                        target=m.name,
                        message=(
                            f"references '{tbl}'[{col}] but that column is not on "
                            f"table '{tbl}' in the plan."
                        ),
                    )
                )
                continue
            src_tbl = resolve_source_key(t, schema_tables)
            wanted.setdefault((tbl, src_tbl), set()).add(src_col)

    for (plan_tbl, src_tbl), cols in sorted(wanted.items()):
        if progress_callback:
            progress_callback(f"  probing {src_tbl} ({len(cols)} column(s))")
        col_list = sorted(cols)
        select = ", ".join(f"COUNT([{c}]) AS c{i}" for i, c in enumerate(col_list))
        parts = src_tbl.split(".")
        qualified = ".".join(f"[{p}]" for p in parts)
        sql = f"SELECT COUNT(*) AS total, {select} FROM {qualified}"
        rows, err = sql_probe(sql)
        if err:
            out.append(
                VerifyIssue(
                    severity="error",
                    category="live_sql",
                    target=plan_tbl,
                    message=(
                        f"warehouse probe failed for {src_tbl}: {err[:200]} "
                        f"(columns probed: {', '.join(col_list)})"
                    ),
                )
            )
            continue
        if not rows:
            continue
        row = rows[0]
        total = row[0] or 0
        if total == 0:
            out.append(
                VerifyIssue(
                    severity="warning",
                    category="live_sql",
                    target=plan_tbl,
                    message=(
                        f"source table {src_tbl} has 0 rows - every measure over "
                        f"it will return blank."
                    ),
                )
            )
            continue
        for i, c in enumerate(col_list):
            nonnull = row[i + 1] or 0
            if nonnull == 0:
                out.append(
                    VerifyIssue(
                        severity="warning",
                        category="live_sql",
                        target=f"{plan_tbl}[{c}]",
                        message=(
                            f"column is 100% NULL across {total:,} rows - measures "
                            f"aggregating it will return blank."
                        ),
                    )
                )
    return out


def collect_live_dax_issues(
    plan: SchemaPlan,
    xmla_server: str,
    xmla_database: str,
    progress_callback: Callable[[str], None] | None = None,
    runner=None,
) -> list[VerifyIssue]:
    if not xmla_server or not xmla_database:
        return []
    try:
        from model.dax_live_validate import validate_dax_live
    except ImportError:
        return []

    out: list[VerifyIssue] = []
    pending = [
        (m.host_table, m.name, m.dax)
        for m in plan.measures
        if (m.dax or "").strip() and m.dax.strip().upper() != "BLANK()"
    ]
    for host, name, dax in pending:
        others = [p for p in pending if p[1] != name]
        if progress_callback:
            progress_callback(f"  live DAX probe: {name}")
        try:
            err = validate_dax_live(
                xmla_server,
                xmla_database,
                "measure",
                host,
                name,
                dax,
                pending_measures=others,
                runner=runner,
            )
        except Exception as e:
            logger.warning("live DAX probe crashed for %s: %s", name, e)
            continue
        if err:
            msg = err
            for strip in (
                "ERROR - live DAX validation failed: ",
                "ERROR — live DAX validation failed: ",
                " Fix the definition and re-propose.",
            ):
                msg = msg.replace(strip, "")
            out.append(
                VerifyIssue(
                    severity="error",
                    category="live_dax",
                    target=name,
                    message=msg.strip(),
                )
            )
    return out


REPAIR_SYSTEM_PROMPT = """You are a Power BI semantic-model repair agent. A model was generated, then verified against the real warehouse and a business-rule validator. The verification FAILED on the issues listed below. Your job is to emit a JSON PATCH that fixes them.

You never write DAX. You edit the PLAN. Measures whose `intent` you change get their DAX regenerated from scratch by a downstream agent, so a precise `new_intent` IS the fix.

## Output

Return ONLY this JSON object. No prose, no markdown fences. First character MUST be `{`.

{
  "reason": "one sentence naming the root cause you are fixing",
  "operations": [ ... ]
}

## Operation kinds you may emit

- {"kind": "update_measure", "name": "...", "new_intent": "...", "new_format_string": "..."}
  Changing `new_intent` CLEARS the DAX and forces regeneration. This is your primary tool.
- {"kind": "drop_measure", "name": "...", "reason": "..."}
- {"kind": "add_measure", "name": "...", "host_table": "<must be an existing plan table>", "intent": "...", "format_string": "...", "display_folder": "..."}
- {"kind": "rename_measure", "old_name": "...", "new_name": "..."}
- {"kind": "add_column", "table": "...", "column": {"name": "...", "source_column": "...", "data_type": "..."}}
- {"kind": "update_column", "table": "...", "name": "...", "new_data_type": "...", "new_is_hidden": true}
- {"kind": "add_relationship", "from_table": "...", "from_column": "...", "to_table": "...", "to_column": "..."}
- {"kind": "drop_relationship", "from_table": "...", "from_column": "...", "to_table": "...", "to_column": "..."}

## How to fix each issue category

- **placeholder** — DAX generation failed outright. The intent was probably too vague or referenced something that does not exist. Rewrite `new_intent` naming the EXACT plan table and column to aggregate, e.g. "SUM of 'factpolicy'[writtenPremium]". If no column in the plan can support it, `drop_measure` instead and say why in `reason`.
- **live_sql** (column not on table) — the measure references a column that is not in the plan. Either `add_column` if it genuinely exists in the warehouse schema shown below, or rewrite `new_intent` to use a column that IS on that table.
- **live_sql** (warehouse probe failed) — the plan's source table/column names do not match the warehouse. Fix via `update_column` / `add_column`, or drop the measures that depend on it.
- **live_dax** — the DAX executed against the real model and errored. Rewrite `new_intent` to be unambiguous about the aggregation, the table, and any filter.
- **business:column_ref / business:measure_ref** — the DAX points at a name that does not exist. Rewrite the intent to name a real one.
- **business:syntax / business:self_ref** — the generated DAX was malformed or self-referential. Rewrite `new_intent` more simply and explicitly.
- **business:time_intel** — a time-intelligence measure has no usable date dimension. Either add the relationship to the date table, or drop the measure.
- **coverage** — a KPI from the signed-off spec has no measure. `add_measure` it on the fact table that hosts the underlying amount. Pick `host_table` from the plan tables listed below.

## Hard rules

1. NEVER invent a table that is not in the plan or the warehouse schema.
2. NEVER emit an `add_measure` whose `host_table` is absent from the plan's tables.
3. Fix the ROOT CAUSE. If eight measures all fail because one column was misnamed, emit ONE `update_column`, not eight `update_measure`s.
4. If an issue genuinely cannot be fixed with the available source data, `drop_measure` it and name the missing data in `reason`. Shipping a broken measure is worse than shipping fewer measures.
5. Keep every `intent` concrete enough that a DAX generator needs no guesswork: name the table, the column, and the aggregation.
6. Do not touch anything the issue list does not mention.
"""


def _issues_for_prompt(issues: list[VerifyIssue], limit: int = 60) -> str:
    lines: list[str] = []
    for i in issues[:limit]:
        lines.append(f"- [{i.category}] {i.target}: {i.message}")
    if len(issues) > limit:
        lines.append(f"- ... +{len(issues) - limit} more of the same kind")
    return "\n".join(lines)


def _plan_for_repair_prompt(plan: SchemaPlan) -> str:
    lines = ["## Plan tables (host_table must come from this list)"]
    for t in plan.tables:
        cols = ", ".join(f"{c.name}:{c.data_type}" for c in t.columns)
        lines.append(f"- {t.name} [{t.kind}]: {cols}")
    lines.append("")
    lines.append("## Plan measures")
    for m in plan.measures:
        state = "BLANK" if (m.dax or "").strip().upper() in ("", "BLANK()") else "ok"
        lines.append(
            f"- {m.name} (host={m.host_table}, dax={state}) intent={(m.intent or '')[:100]}"
        )
    return "\n".join(lines)


def generate_repair_patch(
    plan: SchemaPlan,
    issues: list[VerifyIssue],
    schema_text: str = "",
    project_id: int | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> dict:
    errors = [i for i in issues if i.severity == "error"]
    if not errors:
        return {"operations": []}

    schema_block = ""
    if schema_text:
        schema_block = (
            "## Warehouse schema (authoritative - only these tables/columns exist)\n"
            + schema_text[:20000]
            + "\n\n"
        )

    user_msg = (
        f"{_plan_for_repair_prompt(plan)}\n\n"
        f"{schema_block}"
        f"## Verification failures to fix ({len(errors)} error(s))\n"
        f"{_issues_for_prompt(errors)}\n\n"
        f"Emit the JSON patch that fixes these."
    )

    raw, _usage = stream_collect(
        REPAIR_SYSTEM_PROMPT,
        user_msg,
        agent="semantic_model",
        max_tokens=8000,
        progress_callback=progress_callback,
        progress_label="repair patch",
    )
    try:
        patch = json.loads(_strip_json_fences(raw))
    except json.JSONDecodeError as e:
        logger.warning("repair patch was not valid JSON: %s", e)
        return {"operations": [], "reason": f"repairer returned invalid JSON: {e}"}
    if not isinstance(patch, dict):
        return {"operations": [], "reason": "repairer returned a non-object"}
    patch.setdefault("operations", [])
    return patch


@dataclass
class VerifyLoopResult:

    plan: SchemaPlan
    rounds_run: int = 0
    stopped_reason: str = ""
    final_issues: list[VerifyIssue] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    coverage_summary: str = ""

    @property
    def final_errors(self) -> list[VerifyIssue]:
        return [i for i in self.final_issues if i.severity == "error"]

    @property
    def final_warnings(self) -> list[VerifyIssue]:
        return [i for i in self.final_issues if i.severity == "warning"]


def run_verify_repair_loop(
    plan: SchemaPlan,
    *,
    schema_tables: dict[str, list[tuple[str, str]]],
    schema_text: str = "",
    profile: dict | None = None,
    kpi_requirements_block: str = "",
    populate_measures_fn: Callable | None = None,
    project_id: int | None = None,
    sql_probe: SqlProbe | None = None,
    xmla_target: tuple[str, str] | None = None,
    max_rounds: int = MAX_ROUNDS_HARD_CAP,
    log: Callable[[str], None] | None = None,
) -> VerifyLoopResult:
    from .refine import apply_plan_patch, validate_patched_plan

    def _log(msg: str) -> None:
        if log:
            log(msg)
        else:
            logger.info(msg)

    result = VerifyLoopResult(plan=plan)

    def _collect_all(p: SchemaPlan) -> tuple[list[VerifyIssue], str]:
        found: list[VerifyIssue] = []
        found.extend(collect_placeholder_issues(p))
        found.extend(collect_business_issues(p, profile))
        cov_issues, cov_summary = collect_coverage_issues(p, kpi_requirements_block)
        found.extend(cov_issues)
        if sql_probe is not None:
            try:
                found.extend(
                    collect_sql_probe_issues(p, schema_tables, sql_probe, _log)
                )
            except Exception as e:
                _log(f"  warehouse probing skipped: {e}")
        if xmla_target:
            try:
                found.extend(
                    collect_live_dax_issues(p, xmla_target[0], xmla_target[1], _log)
                )
            except Exception as e:
                _log(f"  live DAX probing skipped: {e}")
        deduped: dict[tuple, VerifyIssue] = {}
        for i in found:
            deduped.setdefault(i.key(), i)
        return list(deduped.values()), cov_summary

    issues, coverage_summary = _collect_all(plan)
    result.coverage_summary = coverage_summary
    prev_error_count: int | None = None

    while True:
        errors = [i for i in issues if i.severity == "error"]
        _log(
            f"Phase 8 (round {result.rounds_run}): {len(errors)} error(s), "
            f"{len(issues) - len(errors)} warning(s)"
        )
        if not errors:
            result.stopped_reason = "clean - no blocking issues remain"
            break
        if result.rounds_run >= max_rounds:
            result.stopped_reason = (
                f"hit the {max_rounds}-round safety cap with {len(errors)} "
                f"error(s) unresolved"
            )
            break
        if prev_error_count is not None and len(errors) >= prev_error_count:
            result.stopped_reason = (
                f"no progress - error count went {prev_error_count} -> {len(errors)}"
            )
            break
        prev_error_count = len(errors)

        _log(f"Phase 8: asking the repair agent to fix {len(errors)} issue(s)...")
        patch = generate_repair_patch(
            plan,
            issues,
            schema_text=schema_text,
            project_id=project_id,
            progress_callback=log,
        )
        ops = patch.get("operations") or []
        if not ops:
            result.stopped_reason = "repair agent proposed no changes" + (
                f" ({patch.get('reason')})" if patch.get("reason") else ""
            )
            break
        if patch.get("reason"):
            _log(f"Phase 8: root cause - {patch['reason']}")

        new_dict, action_log, op_errors = apply_plan_patch(plan_to_dict(plan), patch)
        for oe in op_errors[:10]:
            _log(f"  skipped op: {oe}")
        if not action_log:
            result.stopped_reason = "every repair operation was rejected as invalid"
            break

        plan_errors, _warns = validate_patched_plan(new_dict, schema_tables, profile)
        if plan_errors:
            result.stopped_reason = (
                f"repair discarded - it introduced {len(plan_errors)} plan error(s)"
            )
            _log(f"Phase 8: {result.stopped_reason}; keeping the previous plan")
            break

        from .plan import dict_to_plan

        plan = dict_to_plan(new_dict)
        result.actions.extend(action_log)
        for a in action_log[:12]:
            _log(f"  {a}")
        if len(action_log) > 12:
            _log(f"  (+{len(action_log) - 12} more repair actions)")

        if populate_measures_fn is not None:
            blank_before = sum(
                1
                for m in plan.measures
                if (m.dax or "").strip().upper() in ("", "BLANK()")
            )
            if blank_before:
                _log(f"Phase 8: regenerating DAX for {blank_before} measure(s)...")
                plan, _failures = populate_measures_fn(
                    plan, profile=profile, project_id=project_id
                )

        result.rounds_run += 1
        issues, coverage_summary = _collect_all(plan)
        if coverage_summary:
            result.coverage_summary = coverage_summary

    result.plan = plan
    result.final_issues = issues
    _log(
        f"Phase 8: stopped after {result.rounds_run} repair round(s) - {result.stopped_reason}"
    )
    return result
