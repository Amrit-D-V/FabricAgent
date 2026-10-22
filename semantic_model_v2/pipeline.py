from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

_TABULAR_EDITOR_PATHS = [
    r"C:\Program Files (x86)\Tabular Editor\TabularEditor.exe",
    r"C:\Program Files\Tabular Editor\TabularEditor.exe",
    r"C:\Program Files\Tabular Editor 3\TabularEditor3.exe",
]


def _find_tabular_editor() -> str | None:
    for p in _TABULAR_EDITOR_PATHS:
        if os.path.isfile(p):
            return p
    return None


from .bpa_validate import run_bpa
from .business_validate import validate_measures_business
from .critic import apply_critic_patch, critique_plan
from .kpi_backfill import seed_measures_from_kpi_block
from .measure_generator import populate_measures
from .measure_inject import inject_standard_measures
from .plan import SchemaPlan, plan_to_dict
from .plan_fixups import apply_plan_fixups, apply_post_dax_fixups
from .plan_generator import generate_plan
from .repair import auto_repair_plan, format_repair_log
from .shift_context import expand_shift_context
from .skeleton import build_skeleton
from .validate import PlanError, format_errors, validate_plan
from .verify_loop import collect_coverage_issues, run_verify_repair_loop


def _profile_date_range(profile: dict | None) -> tuple[int, int] | None:
    if not profile:
        return None
    from datetime import date, datetime

    min_d: date | None = None
    max_d: date | None = None
    for tinfo in profile.values():
        cols = tinfo.get("columns", {})
        for cinfo in cols.values():
            for sample in cinfo.get("samples") or []:
                parsed: date | None = None
                if isinstance(sample, datetime):
                    parsed = sample.date()
                elif isinstance(sample, date):
                    parsed = sample
                elif isinstance(sample, str):
                    for fmt in (
                        "%Y-%m-%d",
                        "%Y-%m-%d %H:%M:%S",
                        "%Y-%m-%dT%H:%M:%S",
                        "%Y-%m-%d %H:%M:%S.%f",
                    ):
                        try:
                            parsed = datetime.strptime(
                                sample[: len(fmt) + 6], fmt
                            ).date()
                            break
                        except (ValueError, TypeError):
                            continue
                if parsed is None:
                    continue
                if parsed.year < 2000:
                    continue
                if min_d is None or parsed < min_d:
                    min_d = parsed
                if max_d is None or parsed > max_d:
                    max_d = parsed
    if min_d is None or max_d is None:
        return None
    return (min_d.year, max_d.year)


def _enforce_explicit_measures(plan: SchemaPlan) -> list[str]:
    import re as _re

    actions: list[str] = []
    if not plan.measures:
        return actions

    referenced: set[tuple[str, str]] = set()
    agg_pat = _re.compile(
        r"\b(?:SUM|AVERAGE|MIN|MAX|COUNT|DISTINCTCOUNT|SUMX|AVERAGEX|MINX|MAXX)\s*\(\s*"
        r"'?([^.'\s\)]+(?:\.[^.'\s\)]+)?)'?\s*\[([^\]]+)\]"
    )
    for m in plan.measures:
        if not m.dax:
            continue
        for tbl, col in agg_pat.findall(m.dax):
            referenced.add((tbl, col))

    for t in plan.tables:
        for c in t.columns:
            if (t.name, c.name) in referenced:
                changed = []
                if c.summarize_by != "none":
                    c.summarize_by = "none"
                    changed.append("summarize_by→none")
                if not c.is_hidden:
                    c.is_hidden = True
                    changed.append("hidden")
                if changed:
                    actions.append(
                        f"'{t.name}'[{c.name}]: {' + '.join(changed)} "
                        f"(used by measure aggregation)"
                    )
    return actions


def _enrich_calculated_dates_from_profile(
    plan: SchemaPlan, profile: dict | None
) -> list[str]:
    actions: list[str] = []
    if not profile:
        return actions
    yr_range = _profile_date_range(profile)
    if not yr_range:
        return actions
    min_year, max_year = yr_range

    for t in plan.tables:
        if t.kind != "calculated":
            continue
        key = next((c for c in t.columns if c.is_key), None)
        if key is None:
            continue
        if key.data_type != "dateTime":
            continue
        if key.dax_expression:
            continue
        dax = f"CALENDAR(DATE({min_year}, 1, 1), DATE({max_year}, 12, 31))"
        key.dax_expression = dax
        actions.append(
            f"Enriched '{t.name}'.{key.name} with profile-derived "
            f"dax_expression={dax} (was empty; fallback would have been CALENDARAUTO)."
        )
    return actions


logger = logging.getLogger(__name__)


def _python_bpa(plan, defn_dir: str):
    from model.bpa_engine import LOCAL_SAFE_RULES, evaluate, from_reference_model

    from .bpa_validate import BPAIssue

    try:
        from .reference_model_loader import load_pbip_reference

        ref = load_pbip_reference(defn_dir)
        report = evaluate(from_reference_model(ref), only=LOCAL_SAFE_RULES)
    except Exception as exc:
        return ([], f"in-process BPA failed: {type(exc).__name__}: {exc}")

    issues = [
        BPAIssue(
            rule_id=group["rule"],
            severity=group["severity"],
            object_path=obj,
            message=group["rule"],
        )
        for key in ("errors", "warnings", "info")
        for group in report.get(key, [])
        for obj in group.get("objects", [])
    ]
    return (issues, f"result=Succeeded (fabricbi-python-bpa) {report.get('summary', '')}")


def _bpa_zero_issue_message(raw: str) -> str:
    reason = (raw or "").strip()
    if not reason or "result=Succeeded" in reason:
        return "Phase 7.5: BPA found 0 model-quality issues - clean model"
    if "TabularEditor.exe not found" in reason:
        return (
            "Phase 7.5: BPA skipped - Tabular Editor 2 not installed "
            "(set TE2_PATH in .env)"
        )
    return f"Phase 7.5: BPA skipped/inconclusive: {reason.splitlines()[0]}"


_REFERENCE_CATEGORIES = ("column_ref", "measure_ref")


def _reference_failures(biz_issues) -> dict[str, str]:
    out: dict[str, str] = {}
    for i in biz_issues:
        if i.category in _REFERENCE_CATEGORIES and i.measure not in out:
            out[i.measure] = i.message
    return out


_PBI_SCHEMA_PLATFORM = (
    "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/"
    "platformProperties/2.0.0/schema.json"
)


def _lint_tmdl(tmdl_files: dict[str, str]) -> list[str]:
    import re

    issues: list[str] = []

    ref_re = re.compile(r"^\s*(?:fromColumn|toColumn):\s*(.+?)\s*$", re.MULTILINE)
    for rel_path, content in tmdl_files.items():
        if not rel_path.endswith("relationships.tmdl"):
            continue
        for m in ref_re.finditer(content):
            ref = m.group(1)
            if ref.startswith("'"):
                end = ref.find("'", 1)
                col_part = ref[end + 2 :] if end != -1 else ref
            else:
                col_part = ref.split(".", 1)[1] if "." in ref else ref
            col_part = col_part.strip()
            if (
                col_part
                and not col_part.startswith("'")
                and (" " in col_part or "." in col_part)
            ):
                issues.append(
                    f"[lint] {rel_path}: unquoted column ref `{ref}` — "
                    f"column names with spaces/dots must be wrapped in single quotes"
                )

    key_re = re.compile(r"^\t\tisKey:\s*true\s*$", re.MULTILINE)
    for rel_path, content in tmdl_files.items():
        if not rel_path.startswith("tables/") or not rel_path.endswith(".tmdl"):
            continue
        n_keys = len(key_re.findall(content))
        if n_keys > 1:
            issues.append(
                f"[lint] {rel_path}: {n_keys} columns marked isKey:true — "
                f"Power BI allows at most ONE per table"
            )

    referenced_tables = set()
    for rel_path, content in tmdl_files.items():
        if not rel_path.endswith("relationships.tmdl"):
            continue
        for m in re.finditer(r"toColumn:\s*'?([^.'\s]+(?:\.[^.'\s]+)?)'?\.", content):
            referenced_tables.add(m.group(1))
    for rel_path, content in tmdl_files.items():
        if not rel_path.startswith("tables/") or not rel_path.endswith(".tmdl"):
            continue
        if re.search(
            r"partition\s+\S+\s*=\s*calculated\s*\n\s*mode:\s*import\s*\n\s*source\s*=\s*\{\s*\}",
            content,
        ):
            table_name = os.path.basename(rel_path).replace(".tmdl", "")
            if table_name in referenced_tables:
                issues.append(
                    f"[lint] {rel_path}: calculated table referenced by a relationship "
                    f"has empty `source = {{}}` — PBI will fail with 'invalid column ID'. "
                    f"Use CALENDARAUTO() or a real DAX expression."
                )

    for rel_path, content in tmdl_files.items():
        for ln_no, ln in enumerate(content.splitlines(), 1):
            stripped = ln.lstrip()
            if stripped.startswith("//") and not stripped.startswith("///"):
                issues.append(
                    f"[lint] {rel_path}:{ln_no}: bare `// ` comment — "
                    f"TMDL only allows `///` doc-comments. Remove this line."
                )
                break
        for ln_no, ln in enumerate(content.splitlines(), 1):
            if re.match(r"^\s*description:\s*\S", ln):
                issues.append(
                    f"[lint] {rel_path}:{ln_no}: bare `description:` property — "
                    f"TMDL uses `///` comment-style descriptions above the object."
                )
                break

    return issues


TE_SKIP_PREFIX = (
    "[te] external validation SKIPPED - model NOT validated by Tabular Editor"
)


def _tabular_editor_validate(sm_dir: str) -> tuple[bool, str, bool]:
    te = _find_tabular_editor()
    if not te:
        return True, f"{TE_SKIP_PREFIX} (Tabular Editor not installed)", False
    def_dir = os.path.join(sm_dir, "definition")
    if not os.path.isdir(def_dir):
        return False, f"[te] definition/ folder not found at {def_dir}", True
    try:
        proc = subprocess.run(
            [te, def_dir, "-V"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except Exception as e:
        return True, f"{TE_SKIP_PREFIX} (subprocess failed: {e})", False
    out = (proc.stdout + proc.stderr).strip()
    failed = (
        "result=Failed" in out or "[error]" in out.lower() or "error;" in out.lower()
    )
    if failed:
        return False, f"[te] Tabular Editor rejected model:\n{out}", True
    tail = out.splitlines()[-2] if len(out.splitlines()) >= 2 else out
    return True, f"[te] external validation ran clean: {tail}", True


def save_pipeline_output(
    result: PipelineResult,
    output_base: str,
    model_name: str,
    server_fqdn: str = "",
    db_name: str = "",
    with_report_scaffold: bool = True,
) -> str:
    if not result.tmdl_files:
        raise ValueError("PipelineResult has no tmdl_files — Phase 7 didn't run")

    lint_issues = _lint_tmdl(result.tmdl_files)
    if lint_issues:
        raise ValueError(
            "TMDL pre-flight lint failed — fix the renderer before saving:\n  "
            + "\n  ".join(lint_issues)
        )

    sm_dir = os.path.join(output_base, f"{model_name}.SemanticModel")
    rpt_dir = os.path.join(output_base, f"{model_name}.Report")
    def_dir = os.path.join(sm_dir, "definition")

    if os.path.isdir(def_dir):
        shutil.rmtree(def_dir)
    os.makedirs(os.path.join(def_dir, "tables"), exist_ok=True)

    def _w(path: str, data) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2) if not isinstance(data, str) else data)

    pbip_path = os.path.join(output_base, f"{model_name}.pbip")
    _w(
        pbip_path,
        {
            "version": "1.0",
            "artifacts": [{"report": {"path": f"{model_name}.Report"}}],
            "settings": {},
        },
    )

    _w(
        os.path.join(sm_dir, ".platform"),
        {
            "$schema": _PBI_SCHEMA_PLATFORM,
            "metadata": {"type": "SemanticModel", "displayName": model_name},
            "config": {"version": "2.0", "logicalId": str(uuid.uuid4())},
        },
    )
    _w(os.path.join(sm_dir, "definition.pbism"), {"version": "4.1", "settings": {}})

    for rel_path, content in result.tmdl_files.items():
        filled = content.replace(
            "SERVER_FQDN", server_fqdn or "placeholder.database.fabric.microsoft.com"
        ).replace("DB_NAME", db_name or "PlaceholderDB")
        full = os.path.join(def_dir, rel_path.replace("/", os.sep))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(filled if filled.endswith("\n") else filled + "\n")

    if with_report_scaffold:
        rpt_def = os.path.join(rpt_dir, "definition")
        os.makedirs(
            os.path.join(rpt_def, "pages", "blank_page", "visuals"), exist_ok=True
        )
        _w(
            os.path.join(rpt_dir, ".platform"),
            {
                "$schema": _PBI_SCHEMA_PLATFORM,
                "metadata": {"type": "Report", "displayName": model_name},
                "config": {"version": "2.0", "logicalId": str(uuid.uuid4())},
            },
        )
        _w(
            os.path.join(rpt_dir, "definition.pbir"),
            {
                "version": "4.0",
                "datasetReference": {
                    "byPath": {"path": f"../{model_name}.SemanticModel"}
                },
            },
        )
        _w(
            os.path.join(rpt_def, "report.json"),
            {
                "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/report/1.2.0/schema.json",
                "themeCollection": {
                    "baseTheme": {
                        "name": "CY24SU06",
                        "reportVersionAtImport": "5.55",
                        "type": "SharedResources",
                    }
                },
                "settings": {
                    "useStylableVisualContainerHeader": True,
                    "defaultDrillFilterOtherVisuals": True,
                    "allowChangeFilterTypes": True,
                    "useEnhancedTooltips": True,
                    "useDefaultAggregateDisplayName": True,
                },
                "layoutOptimization": "None",
            },
        )
        _w(
            os.path.join(rpt_def, "version.json"),
            {
                "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/versionMetadata/1.0.0/schema.json",
                "version": "2.0.0",
            },
        )
        _w(
            os.path.join(rpt_def, "pages", "pages.json"),
            {
                "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/pagesMetadata/1.0.0/schema.json",
                "pageOrder": ["blank_page"],
                "activePageName": "blank_page",
            },
        )
        _w(
            os.path.join(rpt_def, "pages", "blank_page", "page.json"),
            {
                "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/2.1.0/schema.json",
                "name": "blank_page",
                "displayName": "Untitled",
                "displayOption": "FitToPage",
                "height": 720,
                "width": 1280,
            },
        )

    ok, te_msg, te_ran = _tabular_editor_validate(sm_dir)
    if not te_ran:
        logger.warning(te_msg)
    else:
        logger.info(te_msg)
    result.phase_log.append(te_msg)
    if not ok:
        raise RuntimeError(
            f"Saved PBIP but Tabular Editor refused to load it. "
            f"This will fail in Power BI Desktop too — see error below.\n{te_msg}"
        )

    return pbip_path


@dataclass
class PipelineResult:

    plan: SchemaPlan | None
    plan_dict: dict | None
    tmdl_files: dict[str, str] = field(default_factory=dict)
    plan_errors: list[PlanError] = field(default_factory=list)
    measure_failures: list[tuple[str, str]] = field(default_factory=list)
    business_errors: list[str] = field(default_factory=list)
    phase_log: list[str] = field(default_factory=list)
    succeeded: bool = False
    error_message: str = ""
    ready_to_deploy: bool = False
    blocking_issues: list[str] = field(default_factory=list)
    verify_rounds: int = 0
    verify_stopped_reason: str = ""
    verify_actions: list[str] = field(default_factory=list)
    verify_issues: list[str] = field(default_factory=list)


def generate_model_v2(
    schema_text: str,
    schema_tables: dict[str, list[tuple[str, str]]],
    profile: dict | None,
    profile_context: str,
    requirements: str,
    model_name: str,
    server_fqdn: str,
    db_name: str,
    rules_context: str = "",
    template_library: str = "",
    project_id: int | None = None,
    approved_plan: SchemaPlan | None = None,
    progress_callback: Callable[[str], None] | None = None,
    kpi_requirements_block: str = "",
    reference_model_block: str = "",
    sql_probe=None,
    xmla_target: tuple[str, str] | None = None,
    verify_loop_enabled: bool = True,
) -> PipelineResult:
    result = PipelineResult(plan=None, plan_dict=None)

    def log(msg: str) -> None:
        result.phase_log.append(msg)
        logger.info(f"[v2 pipeline] {msg}")
        if progress_callback:
            progress_callback(msg)

    try:
        if approved_plan is None:
            log("Phase 3: asking the LLM for a structured plan...")
            plan, plan_errors, _raw = generate_plan(
                schema_text=schema_text,
                profile_context=profile_context,
                requirements=requirements,
                model_name=model_name,
                server_fqdn=server_fqdn,
                db_name=db_name,
                schema_tables=schema_tables,
                profile=profile,
                rules_context=rules_context,
                template_library=template_library,
                project_id=project_id,
                progress_callback=progress_callback,
                kpi_requirements_block=kpi_requirements_block,
                reference_model_block=reference_model_block,
            )

            log("Phase 3.5: auto-repairing plan inconsistencies...")
            plan, repair_actions = auto_repair_plan(plan, schema_tables)
            if repair_actions:
                log(f"Phase 3.5: applied {len(repair_actions)} repair(s)")
                for line in format_repair_log(repair_actions).splitlines():
                    result.phase_log.append(line)

            if kpi_requirements_block or reference_model_block:
                log("Phase 3.7: critic reviewing plan against requirements...")
                try:
                    patch = critique_plan(
                        plan=plan,
                        requirements=requirements,
                        kpi_requirements_block=kpi_requirements_block,
                        reference_model_block=reference_model_block,
                        progress_callback=progress_callback,
                    )
                    if patch.diagnosis:
                        log(f"Phase 3.7: diagnosis — {patch.diagnosis}")
                    if not patch.is_empty:
                        plan, patch_actions = apply_critic_patch(
                            plan, patch, schema_tables
                        )
                        log(f"Phase 3.7: applied critic patch ({patch.summary()})")
                        for action_line in patch_actions:
                            result.phase_log.append(f"  {action_line}")
                    else:
                        log("Phase 3.7: critic found no gaps")
                    for note in patch.notes:
                        result.phase_log.append(f"  note: {note}")
                except Exception as e:
                    log(f"Phase 3.7: critic skipped due to error: {e}")

            try:
                plan, shift_result = expand_shift_context(
                    plan=plan,
                    requirements=requirements,
                    kpi_requirements_block=kpi_requirements_block,
                    reference_model_block=reference_model_block,
                )
                if shift_result.triggered:
                    log(
                        f"Phase 3.8: shift-context expansion — {shift_result.summary} ({shift_result.reason})"
                    )
                    for base, new in shift_result.cloned_measures[:20]:
                        result.phase_log.append(f"  + {new}")
                    if len(shift_result.cloned_measures) > 20:
                        result.phase_log.append(
                            f"  ... +{len(shift_result.cloned_measures)-20} more"
                        )
            except Exception as e:
                log(f"Phase 3.8: shift-context expansion skipped due to error: {e}")

            try:
                inject_report = inject_standard_measures(plan)
                if inject_report.count:
                    log(
                        f"Phase 3.85: auto-injected {inject_report.count} "
                        f"standard business measures across "
                        f"{len({h for h, _ in inject_report.injected})} table(s)"
                    )
                    for host, name in inject_report.injected[:25]:
                        result.phase_log.append(f"  + {host} :: {name}")
                    if inject_report.count > 25:
                        result.phase_log.append(
                            f"  ... + {inject_report.count - 25} more measures"
                        )
                else:
                    log(
                        "Phase 3.85: no standard measures to inject (all tables skipped or already covered)"
                    )
            except Exception as e:
                log(f"Phase 3.85: measure injection skipped due to error: {e}")

            plan_errors = validate_plan(plan, schema_tables, profile=profile)
            result.plan = plan
            result.plan_dict = plan_to_dict(plan)
            result.plan_errors = plan_errors

            blocking_errors = [e for e in plan_errors if e.severity == "error"]
            if blocking_errors:
                result.error_message = f"Plan validation failed after auto-repair:\n{format_errors(blocking_errors)}"
                log(
                    f"Phase 4: {len(blocking_errors)} blocking error(s) — stopping for review"
                )
                return result

            log(
                f"Phase 4: plan valid ({len(plan.tables)} tables, "
                f"{len(plan.relationships)} relationships, {len(plan.measures)} measures)"
            )
            log(
                "Awaiting user approval — call generate_model_v2 again with approved_plan"
            )
            result.succeeded = False
            return result
        else:
            plan = approved_plan

            def _table_breakdown(p):
                facts = sum(1 for t in p.tables if t.kind == "fact")
                dims = sum(1 for t in p.tables if t.kind == "dim")
                calcs = sum(1 for t in p.tables if t.kind == "calculated")
                return f"{len(p.tables)} total ({facts} facts · {dims} dims · {calcs} calc)"

            log(
                f"Phase 3 (approved plan input): {_table_breakdown(plan)}, "
                f"{len(plan.relationships)} relationships, {len(plan.measures)} measures"
            )

            log("Phase 3.5: auto-repairing...")
            before_tables = len(plan.tables)
            before_rels = len(plan.relationships)
            before_measures = len(plan.measures)
            plan, repair_actions = auto_repair_plan(plan, schema_tables)
            if repair_actions:
                log(f"Phase 3.5: applied {len(repair_actions)} repair(s)")
                for line in format_repair_log(repair_actions).splitlines():
                    result.phase_log.append(line)
            dropped_tables = before_tables - len(plan.tables)
            dropped_rels = before_rels - len(plan.relationships)
            dropped_measures = before_measures - len(plan.measures)
            if dropped_tables or dropped_rels or dropped_measures:
                log(
                    f"Phase 3.5: dropped {dropped_tables} table(s), "
                    f"{dropped_rels} relationship(s), {dropped_measures} measure(s) "
                    f"during repair. Now: {_table_breakdown(plan)}, "
                    f"{len(plan.relationships)} relationships, {len(plan.measures)} measures."
                )
            else:
                log(
                    f"Phase 3.5: no tables/relationships/measures dropped. "
                    f"Still {_table_breakdown(plan)}."
                )

            if profile:
                log(
                    f"Phase 3.6: data profile available ({len(profile)} table(s)) — "
                    f"date-range enrichment + type coercion enabled"
                )
            else:
                log(
                    "Phase 3.6: NO data profile — date-range enrichment + string→type "
                    "coercion will be skipped (profiling failed or returned nothing)"
                )
            enrich_actions = _enrich_calculated_dates_from_profile(plan, profile)
            for action in enrich_actions:
                log(f"Phase 3.6: {action}")
            if not enrich_actions:
                log("Phase 3.6: no date-dim enrichment needed (table count unchanged).")

            errors = validate_plan(plan, schema_tables, profile=profile)
            blocking_errors = [e for e in errors if e.severity == "error"]
            if blocking_errors:
                result.plan = plan
                result.plan_dict = plan_to_dict(plan)
                result.plan_errors = errors
                result.error_message = (
                    f"Approved plan still has validation errors after auto-repair:\n"
                    f"{format_errors(blocking_errors)}"
                )
                log(
                    f"Phase 4: {len(blocking_errors)} blocking error(s) — table count unchanged at {_table_breakdown(plan)}"
                )
                return result
            result.plan_errors = errors
            log(
                f"Phase 4: validation passed ({len(errors)} warning(s)). "
                f"Table count unchanged: {_table_breakdown(plan)}."
            )

        try:
            fixup_actions = apply_plan_fixups(plan, profile=profile)
            for action in fixup_actions:
                log(f"Phase 4.5: {action}")
            if not fixup_actions:
                log("Phase 4.5: no best-practice fixups needed.")
        except Exception as e:
            log(f"Phase 4.5: best-practice fixups skipped due to error: {e}")

        if kpi_requirements_block:
            try:
                seeded, already, no_host = seed_measures_from_kpi_block(
                    plan, kpi_requirements_block
                )
                if seeded:
                    plan.measures.extend(seeded)
                log(
                    f"Phase 5.5: KPI backfill — seeded {len(seeded)} measure shell(s); "
                    f"{len(already)} already authored by LLM; "
                    f"{len(no_host)} unhostable (no fact table)."
                )
                if no_host:
                    log(
                        f"  Unhostable KPIs: {no_host[:5]}{'...' if len(no_host) > 5 else ''}"
                    )
            except Exception as e:
                log(f"Phase 5.5: KPI backfill skipped due to error: {e}")
        else:
            log("Phase 5.5: KPI backfill skipped (no KPI requirements block attached)")

        log(
            f"Phase 6: generating DAX for {len(plan.measures)} measures (table count unchanged: {_table_breakdown(plan)})..."
        )
        measure_progress = []

        def _measure_progress(i, n, name):
            measure_progress.append(f"  measure {i}/{n}: {name}")
            if progress_callback:
                progress_callback(f"Phase 6: measure {i}/{n} — {name}")

        before_measures = len(plan.measures)
        plan, failures = populate_measures(
            plan,
            profile=profile,
            project_id=project_id,
            progress_callback=_measure_progress,
        )
        result.measure_failures = failures
        if failures:
            log(
                f"Phase 6: {len(failures)} measure(s) failed DAX gen, shipped as BLANK() "
                f"placeholders (fix before deploy): {[f[0] for f in failures]}"
            )
        log(
            f"Phase 6: {len(plan.measures)} measures finalized (was {before_measures}); "
            f"table count unchanged: {_table_breakdown(plan)}."
        )

        try:
            for action in apply_post_dax_fixups(plan):
                log(f"Phase 6.5: {action}")
        except Exception as e:
            log(f"Phase 6.5: post-DAX fixups skipped due to error: {e}")

        if kpi_requirements_block:
            try:
                cov_issues, cov_summary = collect_coverage_issues(
                    plan, kpi_requirements_block
                )
                log(f"Phase 6.6: KPI spec coverage - {cov_summary}")
                if cov_issues:
                    log(
                        f"  [WARN] {len(cov_issues)} spec KPI(s) have no measure - "
                        f"Phase 8 will attempt to author them"
                    )
                    log(f"  First 10 unmatched: {[i.target for i in cov_issues[:10]]}")
                result.phase_log.append(f"  KPI spec coverage: {cov_summary}")
            except Exception as e:
                log(f"Phase 6.6: spec coverage check skipped due to error: {e}")
        else:
            log(
                "Phase 6.6: spec coverage check skipped (no KPI requirements block attached)"
            )
        try:
            biz_issues = validate_measures_business(plan, profile=profile)
            if biz_issues:
                err_count = sum(1 for i in biz_issues if i.severity == "error")
                warn_count = sum(1 for i in biz_issues if i.severity == "warning")
                info_count = sum(1 for i in biz_issues if i.severity == "info")
                ref_bad = _reference_failures(biz_issues)
                result.business_errors = [
                    str(i)
                    for i in biz_issues
                    if i.severity == "error"
                    and i.category not in _REFERENCE_CATEGORIES
                    and i.measure not in ref_bad
                ]
                log(
                    f"Phase 6.7: business validation — "
                    f"{err_count} error · {warn_count} warning · {info_count} info"
                )
                for issue in biz_issues:
                    result.phase_log.append(f"  {issue}")
                if ref_bad:
                    log(
                        f"Phase 6.7: {len(ref_bad)} measure(s) reference names that "
                        f"do not resolve - handing them to Phase 8 for repair "
                        f"(DAX kept for now so the repair agent can see it)"
                    )
            else:
                log("Phase 6.7: business validation passed (no issues)")
        except Exception as e:
            log(f"Phase 6.7: business validation skipped due to error: {e}")

        if verify_loop_enabled:
            log(
                "Phase 8: verifying the model and repairing what fails "
                "(loops until clean or no further progress)..."
            )
            try:
                loop_res = run_verify_repair_loop(
                    plan,
                    schema_tables=schema_tables,
                    schema_text=schema_text,
                    profile=profile,
                    kpi_requirements_block=kpi_requirements_block,
                    populate_measures_fn=populate_measures,
                    project_id=project_id,
                    sql_probe=sql_probe,
                    xmla_target=xmla_target,
                    log=log,
                )
                plan = loop_res.plan
                result.verify_rounds = loop_res.rounds_run
                result.verify_stopped_reason = loop_res.stopped_reason
                result.verify_actions = loop_res.actions
                result.verify_issues = [str(i) for i in loop_res.final_issues]
                if loop_res.coverage_summary:
                    result.phase_log.append(
                        f"  KPI spec coverage after repair: {loop_res.coverage_summary}"
                    )
                log(
                    f"Phase 8: {loop_res.rounds_run} repair round(s), "
                    f"{len(loop_res.actions)} change(s) applied; "
                    f"{len(loop_res.final_errors)} error(s) and "
                    f"{len(loop_res.final_warnings)} warning(s) remain"
                )
                for i in loop_res.final_issues[:15]:
                    result.phase_log.append(f"  {i}")
                if len(loop_res.final_issues) > 15:
                    result.phase_log.append(
                        f"  ... +{len(loop_res.final_issues) - 15} more"
                    )
            except Exception as e:
                logger.exception("verify/repair loop crashed")
                log(f"Phase 8: verify/repair loop skipped due to error: {e}")
                # A crashed verification is not a clean one. Leaving
                # verify_issues empty here made a crash byte-identical to a
                # pass: `ready_to_deploy` stayed True and the UI showed a green
                # tick. Worse, this loop is where collect_sql_probe_issues runs
                # — the pipeline's only check against live warehouse data — so
                # a transient rate limit could erase a real, already-detected
                # failure. Record it as blocking instead.
                result.verify_stopped_reason = f"crashed: {type(e).__name__}"
                result.verify_issues.append(
                    f"[ERROR] verification did not complete ({type(e).__name__}: {e}) "
                    f"— this model is UNVERIFIED, not verified-clean"
                )
        else:
            log("Phase 8: verify/repair loop disabled for this build")

        try:
            biz_issues = validate_measures_business(plan, profile=profile)
            result.business_errors = [
                str(i)
                for i in biz_issues
                if i.severity == "error" and i.category not in _REFERENCE_CATEGORIES
            ]
            ref_bad = _reference_failures(biz_issues)
            if ref_bad:
                already_failed = {n for n, _ in result.measure_failures}
                for m in plan.measures:
                    why = ref_bad.get(m.name)
                    if why is None or m.dax.strip() == "BLANK()":
                        continue
                    m.dax = "BLANK()"
                    if m.name not in already_failed:
                        result.measure_failures.append(
                            (m.name, f"unresolvable reference: {why}")
                        )
                    log(
                        f"Phase 8.5: '{m.name}' {why} - survived repair, DAX "
                        f"replaced with BLANK() placeholder (fix before deploy)"
                    )
            else:
                result.measure_failures = [
                    (n, why)
                    for n, why in result.measure_failures
                    if any(
                        (m.name == n and m.dax.strip().upper() in ("", "BLANK()"))
                        for m in plan.measures
                    )
                ]
        except Exception as e:
            log(f"Phase 8.5: final reference sweep skipped due to error: {e}")

        explicit_actions = _enforce_explicit_measures(plan)
        if explicit_actions:
            log(
                f"Phase 6.5: hardened {len(explicit_actions)} column(s) "
                f"behind explicit measures (summarize_by=none + is_hidden=true)"
            )
            for a in explicit_actions[:10]:
                result.phase_log.append(f"  {a}")
            if len(explicit_actions) > 10:
                result.phase_log.append(f"  (+ {len(explicit_actions) - 10} more)")

        log("Phase 7: rendering deterministic TMDL skeleton...")
        tmdl_files = build_skeleton(
            plan, progress_callback=progress_callback, profile=profile
        )
        result.tmdl_files = tmdl_files
        rendered_table_count = sum(1 for k in tmdl_files if k.startswith("tables/"))
        log(
            f"Phase 7: rendered {len(tmdl_files)} TMDL files "
            f"({rendered_table_count} table files for {len(plan.tables)} plan tables)."
        )
        if rendered_table_count != len(plan.tables):
            log(
                f"⚠ Phase 7: table-file count ({rendered_table_count}) does NOT match "
                f"plan table count ({len(plan.tables)}) — renderer bug, please report."
            )

        try:
            import tempfile as _tempfile

            with _tempfile.TemporaryDirectory(prefix="bpa_") as _tmp:
                _defn = os.path.join(_tmp, "definition")
                for rel, content in tmdl_files.items():
                    out_path = os.path.join(_defn, rel.replace("/", os.sep))
                    os.makedirs(os.path.dirname(out_path), exist_ok=True)
                    with open(out_path, "w", encoding="utf-8") as fh:
                        fh.write(content)
                issues, raw = run_bpa(_defn, timeout_sec=60)
                if not issues and "TabularEditor.exe not found" in (raw or ""):
                    # TE2 is Windows-only and is never present on the VM or in a
                    # container, so this branch is the DEFAULT in production.
                    # Fall back to the in-process rules rather than skip the
                    # phase entirely — a skipped check reads as a clean model.
                    issues, raw = _python_bpa(plan, _defn)
                if issues:
                    by_sev: dict[str, int] = {}
                    for i in issues:
                        by_sev[i.severity] = by_sev.get(i.severity, 0) + 1
                    breakdown = " · ".join(f"{n} {s}" for s, n in by_sev.items())
                    log(
                        f"Phase 7.5: BPA found {len(issues)} model-quality issue(s) ({breakdown})"
                    )
                    for i in issues[:10]:
                        result.phase_log.append(f"  {i}")
                    if len(issues) > 10:
                        result.phase_log.append(f"  ... +{len(issues)-10} more")
                else:
                    log(_bpa_zero_issue_message(raw))
        except Exception as e:
            log(f"Phase 7.5: BPA skipped due to error: {e}")

        result.plan = plan
        result.plan_dict = plan_to_dict(plan)
        result.succeeded = True
        blocking: list[str] = []
        if result.measure_failures:
            blocking.append(
                f"{len(result.measure_failures)} measure(s) shipped as BLANK() placeholders: "
                + ", ".join(n for n, _ in result.measure_failures[:8])
            )
        if result.business_errors:
            blocking.append(
                f"{len(result.business_errors)} business-validation error(s)"
            )
        _verify_errors = [i for i in result.verify_issues if i.startswith("[ERROR]")]
        if _verify_errors:
            blocking.append(
                f"{len(_verify_errors)} issue(s) survived the verify/repair loop "
                f"({result.verify_stopped_reason})"
            )
        _blocking_plan = [
            e for e in result.plan_errors if getattr(e, "severity", "") == "error"
        ]
        if _blocking_plan:
            blocking.append(f"{len(_blocking_plan)} blocking plan error(s)")
        from .validate import UNLOADABLE_TYPE_MARKER

        _unloadable = [
            e
            for e in result.plan_errors
            if UNLOADABLE_TYPE_MARKER in getattr(e, "message", "")
        ]
        if _unloadable:
            blocking.append(
                f"{len(_unloadable)} column(s) will not load — text source typed as "
                f"a number/date on a key: "
                + ", ".join(getattr(e, "where", "?") for e in _unloadable[:6])
            )
        from .validate import date_table_gate

        blocking.extend(date_table_gate(plan))
        result.blocking_issues = blocking
        result.ready_to_deploy = not blocking
        return result

    except Exception as e:
        logger.exception("v2 pipeline crashed")
        result.error_message = f"{type(e).__name__}: {e}"
        log(f"FAILED: {result.error_message}")
        return result
