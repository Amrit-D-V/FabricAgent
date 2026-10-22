import asyncio
import glob
import json
import os
import re
import shutil
import subprocess
import time

from auth.token_manager import get_token_cached
from config import (
    DAX_LIVE_VALIDATE,
    PROJECT_ROOT,
    REPORT_LAYOUT,
    REPORT_PAGE_BG,
)
from core.context import state
from fabric import workspace_context as _ws_ctx
from fabric.model_metadata import prefetch_metadata
from fabric.workspace_context import (
    _active_models,
    _active_palette,
    _active_workspace,
    _load_column_registry,
    _load_measure_registry,
    _load_registry_meta,
)
from llm.pbi_tool_specs import PBI_CLI
from model.build_trace import _build_trace, _snapshot_report, _trace_file_after_write
from model.json_io import _atomic_write_json
from model.tool_guards import (
    _VISUAL_EDIT_TOOLS,
    _apply_alt_text,
    _apply_default_sort,
    _guard_tool_inputs,
    _list_page_visual_names,
    _pbi_exit_error,
    _pbi_visual_bindings,
    _registered_theme,
    _require_xywh,
    _snap_px,
    _validate_visual_bind_combo,
    _wire_tooltip_page,
)
from report.dashboard_audit import _audit_dashboard
from report.dashboard_polish import inject_theme
from report.health_check_runner import _health_check_report
from report.scan_cache import _REPORT_SCAN_READERS, _invalidate_report_scan
from util.logger_setup import get_logger

logger = get_logger(__name__)

# pbi_cli ships a wrong role map for combo charts (its visual_backend.py
# ROLE_ALIASES maps "column"/"value" -> "ColumnY" and "line" -> "LineY").
# Power BI has no such roles: the real ones are Y (Desktop's "Column y-axis")
# and Y2 ("Line y-axis"). Desktop silently drops unknown roles, so every combo
# chart bound through pbi_cli reported success and rendered as a blank box.
# Corrected here rather than in the venv package so the fix survives a
# pbi_cli reinstall.
_PBIR_ROLE_FIXUPS: dict[str, dict[str, str]] = {
    "lineStackedColumnComboChart": {"ColumnY": "Y", "LineY": "Y2"},
    "lineClusteredColumnComboChart": {"ColumnY": "Y", "LineY": "Y2"},
    # Same class of error: pbi_cli aliases treemap's "category" to "Category",
    # but a treemap groups on "Group" — verified against the two Desktop-authored
    # treemaps in the MavenMarket report, both of which use Group/Values.
    "treemap": {"Category": "Group"},
}

# NOT fixed up here, deliberately: pbi_cli maps `legend` -> "Legend" for
# cartesian charts, and the vendored catalog reference says the PBIR role is
# "Series" with "Legend" only its display name. That would make it the same bug
# again — but no report on disk has a legend bound, so there is no positive
# evidence either way, and renaming a role that turns out to be valid would
# break working bindings rather than fix broken ones. Bind a legend in Desktop,
# inspect the role it writes, and settle it before adding an entry above.

# When pbi_cli cannot map a caller's role alias it writes the caller's own
# parameter name into queryState as the role key. PBIR roles are PascalCase,
# so a lowercase key is always dead weight Desktop ignores. Where the intended
# destination is unambiguous, recover the binding instead of dropping it —
# a gauge's target belongs in TargetValue, not the max-range role pbi_cli
# aliases "target" to.
_PBIR_LOWER_ROLE_FIXUPS: dict[str, dict[str, str]] = {
    "gauge": {"goal": "TargetValue", "target": "TargetValue"},
}


def _overlap_note_for(tool_input: dict, name_key: str = "visual_name") -> str:
    """Warn in the tool result when a placement lands on an existing visual.

    The agent is told to size visuals with pbi_dashboard_layout /
    pbi_layout_compute, but nothing stops it choosing pixel positions itself,
    and "status: created" comes back either way. Reporting the collision here —
    in the result of the call that caused it — is the difference between
    finding out now and finding out from a post-build audit many turns later.
    """
    try:
        from report.layout_guard import overlap_note

        pos = {k: tool_input.get(k) for k in ("x", "y", "width", "height")}
        if any(v is None for v in pos.values()):
            return ""
        return overlap_note(
            tool_input.get("report_path", ""),
            tool_input.get("page", ""),
            str(tool_input.get(name_key) or ""),
            float(pos["x"]),
            float(pos["y"]),
            float(pos["width"]),
            float(pos["height"]),
        )
    except Exception as e:  # never let a warning break the write it describes
        logger.debug("overlap check skipped: %s", e)
        return ""


def normalize_pbir_query_state(vdata: dict) -> bool:
    """Repair a visual.json's queryState in place; True if anything changed.

    Three defects, each of which Power BI Desktop responds to by rendering an
    empty visual rather than raising, so they are invisible without this:
      1. pbi_cli's invented combo-chart roles (ColumnY/LineY -> Y/Y2).
      2. A role alias pbi_cli could not map, written out as the caller's own
         lowercase parameter name ("goal", "field").
      3. Duplicate projections piled up by repeated re-binds of one visual.
    """
    visual = vdata.get("visual", {})
    qs = visual.get("query", {}).get("queryState")
    if not isinstance(qs, dict):
        return False
    vtype = visual.get("visualType", "")
    changed = False

    for bad, good in _PBIR_ROLE_FIXUPS.get(vtype, {}).items():
        if bad not in qs or not isinstance(qs.get(bad), dict):
            continue
        if qs.get(good, {}).get("projections"):
            qs[good]["projections"].extend(qs.pop(bad).get("projections", []))
        else:
            qs[good] = qs.pop(bad)
        changed = True

    for role in list(qs.keys()):
        if not role[:1].islower() or not isinstance(qs.get(role), dict):
            continue
        mapped = _PBIR_LOWER_ROLE_FIXUPS.get(vtype, {}).get(role)
        if mapped and not qs.get(mapped, {}).get("projections"):
            qs[mapped] = qs.pop(role)
            changed = True
            continue
        refs = {p.get("queryRef") for p in qs[role].get("projections", [])}
        elsewhere = {
            p.get("queryRef")
            for other, body in qs.items()
            if other != role and isinstance(body, dict)
            for p in body.get("projections", [])
        }
        # Only drop it when it carries nothing unique — never silently
        # discard the sole copy of a field.
        if not refs or refs <= elsewhere:
            qs.pop(role)
            changed = True

    for body in qs.values():
        if not isinstance(body, dict):
            continue
        projs = body.get("projections", [])
        seen: set = set()
        deduped: list = []
        for p in projs:
            ref = p.get("queryRef", "")
            if ref not in seen:
                seen.add(ref)
                deduped.append(p)
        if len(deduped) < len(projs):
            body["projections"] = deduped
            changed = True

    return changed


def _rmtree_resilient(path: str, attempts: int = 4, delay: float = 0.25) -> bool:
    for i in range(attempts):
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            return True
        except (PermissionError, OSError):
            if i < attempts - 1:
                time.sleep(delay)
                continue
        if not os.path.exists(path):
            return True
    return not os.path.exists(path)


def _coerce_json_object(value):
    for _ in range(2):  # unwrap at most one extra layer of string-encoding
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value.strip():
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                return None
        else:
            return None
    return value if isinstance(value, dict) else None


def _log_hallucination(attempted: list[str], reason: str, tool_input: dict) -> None:
    try:
        from datetime import datetime as _dt

        rec = {
            "timestamp": _dt.now().isoformat(timespec="seconds"),
            "reason": reason,
            "attempted_fields": attempted,
            "visual_name": tool_input.get("visual_name", ""),
            "page": tool_input.get("page", ""),
            "report_path": tool_input.get("report_path", ""),
        }
        path = os.path.join(str(PROJECT_ROOT), "logs", "hallucination_incidents.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        try:
            from db.database import audit as _audit

            _audit(
                "accuracy.field_rejection",
                target=f"{tool_input.get('visual_name', '')} / {tool_input.get('page', '')}",
                details=f"{reason} :: {'; '.join(attempted)}",
            )
        except Exception:
            pass
    except Exception:
        pass


def _validate_bind_fields(tool_input: dict) -> str | None:
    measure_reg = _load_measure_registry()
    column_reg = _load_column_registry()
    meta = _load_registry_meta()

    if not measure_reg and not column_reg:
        msg = (
            "ERROR — bind blocked: the measure/column registry is empty. "
            "Call `pbi_refresh_registry` with the model name(s) you connected "
            "to, then retry the bind. (The registry guards against hallucinated "
            "Table[Field] names reaching pbi-cli unchecked.)"
        )
        _log_hallucination(
            [
                str(tool_input.get(r, ""))
                for r in ("field", "category", "value", "row", "column", "line")
                if tool_input.get(r)
            ],
            "registry_empty",
            tool_input,
        )
        return msg

    # Check for model registry mismatch
    from report.model_identity import registry_mismatch

    _mismatch = registry_mismatch(tool_input.get("report_path", ""))
    if _mismatch:
        _log_hallucination(
            [
                str(tool_input.get(r, ""))
                for r in ("field", "category", "value", "row", "column", "line")
                if tool_input.get(r)
            ],
            "registry_model_mismatch",
            tool_input,
        )
        return f"ERROR — bind blocked: {_mismatch}"

    missing: list[str] = []
    bare_measure_names = {k.split(".", 1)[1] for k in measure_reg if k.startswith("*.")}
    for role in (
        "field",
        "category",
        "value",
        "row",
        "column",
        "line",
        "legend",
        "indicator",
        "trend",
        "goal",
        "max",
        "x",
        "y",
        "detail",
        "size",
    ):
        raw = tool_input.get(role)
        if not raw or "[" not in raw:
            continue
        table = raw.split("[", 1)[0].strip().strip("'").strip('"')
        field = raw.split("[", 1)[1].rstrip("]").strip()
        key = f"{table}.{field}"
        if key in measure_reg or key in column_reg:
            continue
        if field in bare_measure_names:
            continue
        if any(k.endswith(f".{field}") for k in measure_reg):
            continue
        missing.append(f"{role}={raw!r}")

    if not missing:
        return None

    _log_hallucination(missing, "field_not_in_registry", tool_input)

    meta_hint = ""
    if meta.get("model_names"):
        meta_hint = (
            f" Registry covers model(s) {meta['model_names']}, "
            f"last refreshed {meta.get('generated_at', '?')}."
        )
    return (
        "ERROR — refused to bind: the following field(s) do not exist in the "
        "connected model: " + "; ".join(missing) + ". "
        "Call dax_query_operations on INFO.COLUMNS() / INFO.MEASURES() to see "
        "the actual schema, then retry with VERBATIM names. "
        "Common gotchas: `lea_name` (not `LEA Name`), `Student group` (not "
        "`Student Group`). The registry has "
        f"{len(column_reg)} column(s) and {len(measure_reg)} measure(s) loaded."
        + meta_hint
    )


def _validate_format_field(
    table: str, measure: str | None, tool_input: dict
) -> tuple[str | None, str]:
    from report.report_health_checks import validate_registry_field

    err, note = validate_registry_field(
        _load_measure_registry(), _load_column_registry(), table, measure
    )
    if err:
        _log_hallucination(
            [f"{table}[{measure}]" if measure else f"entity={table!r}"],
            "field_not_in_registry",
            tool_input,
        )
    return err, note


def _validate_extension_measure_ref(
    report_path: str, entity: str, measure: str, tool_input: dict
) -> tuple[str | None, str]:
    from report.report_health_checks import validate_extension_measure

    matches = glob.glob(f"{report_path}/**/definition", recursive=True)
    def_dir = next((m for m in matches if os.path.isdir(m)), None)
    # Check both singular and plural report extension filenames
    ext_path = ""
    for _fname in ("reportExtension.json", "reportExtensions.json"):
        _p = os.path.join(def_dir, _fname) if def_dir else ""
        if _p and os.path.isfile(_p):
            ext_path = _p
            break
    ext = None
    if ext_path:
        try:
            with open(ext_path, encoding="utf-8") as f:
                ext = json.load(f)
        except Exception:
            return None, f" (field not verified - {os.path.basename(ext_path)} unreadable)"
    err = validate_extension_measure(ext, entity, measure)
    if err:
        _log_hallucination(
            [f"{entity}[{measure}] (extension)"],
            "field_not_in_registry",
            tool_input,
        )
    return err, ""


def _user_turn_count() -> "int | None":
    """How many user messages this request, or None when unset (MCP/tests)."""
    try:
        msgs = state().get("messages")
        if not isinstance(msgs, list):
            return None
        return sum(1 for m in msgs if isinstance(m, dict) and m.get("role") == "user")
    except Exception:
        return None


def _model_choice_gate(model: str) -> str:
    """Force the user — not the assistant — to choose among several models.

    Naming a model in the tool call is not consent: the assistant will happily
    infer one from project docs (it picked `MavenMarket_Report` from a project
    description while `kaizen_v7` was connected). This gate returns a question
    the first time a model is targeted, and only clears once a NEW user message
    has arrived, so the assistant cannot answer on the user's behalf.

    Returns an error string to surface, or "" when the model is confirmed.
    """
    models = _active_models()
    if len(models) < 2:
        return ""
    turns = _user_turn_count()
    if turns is None:
        return ""  # headless (MCP server / tests) — nothing to ask
    gate = state().get("_model_gate") or {}
    if gate.get("model") == model and turns > gate.get("turn", turns):
        return ""  # user spoke after being asked — treat as confirmed
    if state().get("_model_confirmed") == model:
        return ""
    state()["_model_gate"] = {"model": model, "turn": turns}
    listed = "\n".join(f"  {i}. {m}" for i, m in enumerate(models, 1))
    return (
        f"STOP — {len(models)} semantic models are connected and the user has not "
        f"said which one to use. Do NOT infer it from the project name, docs or "
        f"memory. Ask them now, as a numbered question, and end your turn:\n\n"
        f"QUESTION: Which semantic model should I use?\n{listed}\n"
        f"  {len(models) + 1}. Other — tell me the model name\n\n"
        f"(You proposed {model!r}. After they answer, call this tool again.)"
        + _routing_hint_block(models)
    )


def _last_user_message() -> str:
    try:
        msgs = state().get("messages")
        if not isinstance(msgs, list):
            return ""
        for m in reversed(msgs):
            if isinstance(m, dict) and m.get("role") == "user":
                return str(m.get("content") or "")
    except Exception:
        pass
    return ""


def _routing_hint_block(models) -> str:
    """Schema evidence to show alongside the model question.

    The gate stays a gate — this only stops the user having to guess which
    model their question is about. Any failure here is silent: a routing hint
    is a convenience, and losing it must never block a tool call.
    """
    try:
        from llm.model_router import routing_hint

        hint = routing_hint(_last_user_message(), tuple(models))
        return f"\n\n--- schema evidence (advisory) ---\n{hint}" if hint else ""
    except Exception:
        return ""


def _summarise_pbir(report_dir: str) -> str:
    """Pages / visuals / field bindings for a PBIR report, read from disk.

    Deliberately does NOT shell out to the `pbi` CLI: that binary is not
    installed everywhere (and is absent in the Docker/HF image), which would
    make opening a PBIR report fail with a bare "WinError 2".
    """
    pages_dir = os.path.join(report_dir, "definition", "pages")
    if not os.path.isdir(pages_dir):
        return f"  no definition/pages folder under {report_dir}"

    page_names = sorted(
        d
        for d in os.listdir(pages_dir)
        if os.path.isfile(os.path.join(pages_dir, d, "page.json"))
    )
    lines: list[str] = []
    types: dict[str, int] = {}
    fields: set[str] = set()
    total_visuals = 0
    page_lines: list[str] = []

    for pname in page_names:
        pdir = os.path.join(pages_dir, pname)
        try:
            display = (
                json.loads(
                    open(os.path.join(pdir, "page.json"), encoding="utf-8").read()
                ).get("displayName")
                or pname
            )
        except Exception:
            display = pname
        vdir = os.path.join(pdir, "visuals")
        vis = (
            sorted(
                v
                for v in os.listdir(vdir)
                if os.path.isfile(os.path.join(vdir, v, "visual.json"))
            )
            if os.path.isdir(vdir)
            else []
        )
        total_visuals += len(vis)
        page_lines.append(f"  - {display}  ({len(vis)} visual(s))")
        for v in vis:
            try:
                doc = json.loads(
                    open(os.path.join(vdir, v, "visual.json"), encoding="utf-8").read()
                )
            except Exception as e:
                logger.debug("skipping unreadable visual.json for %s: %s", v, e)
                continue
            vt = (doc.get("visual") or {}).get("visualType") or "?"
            types[vt] = types.get(vt, 0) + 1
            qs = ((doc.get("visual") or {}).get("query") or {}).get("queryState") or {}
            for role in qs.values():
                for proj in (role or {}).get("projections", []) or []:
                    ref = proj.get("queryRef") or proj.get("nativeQueryRef")
                    if ref:
                        fields.add(str(ref))

    lines.append(f"Report (PBIR format): {os.path.basename(report_dir)}")
    lines.append(f"  pages:   {len(page_names)}")
    lines.append(f"  visuals: {total_visuals}")
    lines.append("\nPages:")
    lines += page_lines
    if types:
        lines.append("\nVisual types:")
        lines += [f"  {k}: {v}" for k, v in sorted(types.items(), key=lambda x: -x[1])]
    if fields:
        lines.append(f"\nDistinct field bindings ({len(fields)}):")
        lines += [f"  {f}" for f in sorted(fields)]
    return "\n".join(lines)


def _resolve_target_model(requested: "str | None") -> tuple[str, str]:
    """Pick the semantic model a tool should act on.

    Returns (model, error). Never silently defaults to models[0] when several
    models are connected — acting on the wrong model produces real-looking but
    wrong results, and validation failures that look like bad DAX.
    """
    _models = _active_models()
    name = (requested or "").strip()
    if name:
        if _models and name not in _models:
            _listed = "\n".join(f"  {i}. {m}" for i, m in enumerate(_models, 1))
            return "", (
                f"ERROR — model {name!r} is not connected.\nConnected models:\n{_listed}"
            )
        gate = _model_choice_gate(name)
        if gate:
            return "", gate
        try:
            # Confirmed for the rest of the session; switching models re-gates.
            state()["_model_confirmed"] = name
        except Exception:
            pass
        return name, ""
    if len(_models) == 1:
        return _models[0], ""
    if not _models:
        return "", "ERROR — no semantic model connected: connect one, or pass `model`."
    _listed = "\n".join(f"  {i}. {m}" for i, m in enumerate(_models, 1))
    return "", (
        f"ERROR — {len(_models)} models are connected, so `model` is required. "
        f"Ask the user which one, as a numbered question, then call again with "
        f"`model` set.\nConnected models:\n{_listed}"
    )


# The chat reply is assembled in another process from this one, so a return
# value cannot carry a picture back -- api.chat._drain_visuals reads this log
# once per turn and deletes it.
def _log_visual(name: str, payload: dict) -> None:
    session_id = os.getenv("PBI_AGENT_SESSION_ID")
    if not session_id:
        return
    try:
        import pathlib

        log_dir = pathlib.Path(str(PROJECT_ROOT)) / "bridge_io"
        log_dir.mkdir(exist_ok=True)
        log_file = log_dir / f"session_{session_id}_tools.jsonl"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps({"name": name, "input": payload}) + "\n")
    except Exception:
        pass


def execute_pbi_tool(tool_name: str, tool_input: dict) -> str:
    from auth.tool_access import tool_allowed

    if not tool_allowed(tool_name):
        return "REFUSED — this chat has read-only access; that tool requires an editor."
    if tool_name in ("render_chart", "render_map"):
        _log_visual(tool_name, tool_input)

    _build_trace("tool_call_start", tool=tool_name, input=tool_input)
    try:
        result = _execute_pbi_tool_impl(tool_name, tool_input)
    except Exception as e:
        import traceback as _tb

        _build_trace(
            "tool_call_exception",
            tool=tool_name,
            error=f"{type(e).__name__}: {e}",
            traceback=_tb.format_exc(),
        )
        raise
    _build_trace(
        "tool_call_end",
        tool=tool_name,
        result_chars=len(result) if isinstance(result, str) else 0,
        result_preview=(result[:800] if isinstance(result, str) else str(result)[:800]),
    )
    _rp = tool_input.get("report_path")
    if _rp and tool_name not in _REPORT_SCAN_READERS:
        if _rp.endswith(".pbip"):
            _rp = _rp[:-5] + ".Report"
        _invalidate_report_scan(_rp)
    if isinstance(result, str) and "ACTIVE_REPORT_PATH=" in result:
        for line in result.splitlines():
            if line.startswith("ACTIVE_REPORT_PATH="):
                rp = line.split("=", 1)[1].strip()
                if rp.endswith(".pbip"):
                    rp = rp[:-5] + ".Report"
                if os.path.isdir(rp):
                    _snapshot_report(rp, source=f"after-{tool_name}")
                break
    elif tool_name == "pbi_dashboard_audit":
        rp = tool_input.get("report_path", "")
        if rp and os.path.isdir(rp):
            _snapshot_report(rp, source="after-audit")
    return result


def _execute_pbi_tool_impl(tool_name: str, tool_input: dict) -> str:
    # Checked here, once, rather than in each tool that happens to take a
    # workspace: six of them do, and a seventh added later would silently skip
    # the check. The tools act as FabricBI's service principal, so a workspace the
    # caller cannot open themselves must not be readable through them.
    from auth.tool_access import workspace_refusal

    refusal = workspace_refusal(tool_input.get("workspace"))
    if refusal:
        return refusal

    if tool_name == "render_chart":
        _n = len(tool_input.get("series") or [])
        return f"Chart shown to the user ({tool_input.get('chart_type', '?')}, {_n} series)."
    if tool_name == "render_map":
        _n = len(tool_input.get("points") or [])
        return (
            f"Map shown to the user ({tool_input.get('layer_type', '?')}, {_n} points)."
        )

    if tool_name == "propose_model_edit":
        try:
            from model import model_edit_store as _mes

            # Which model this edit targets. With several models connected we
            # must NOT fall back to models[0]: validating a MavenMarket measure
            # against kaizen_v7 fails for the wrong reason and sends the
            # assistant guessing at table names.
            _target_model, _model_err = _resolve_target_model(tool_input.get("model"))
            if _model_err:
                return _model_err

            otype = tool_input.get("object_type", "")
            name = (tool_input.get("name") or "").strip()
            reason = (tool_input.get("reason") or "").strip()
            operation = (tool_input.get("operation") or "create").strip().lower()
            if operation not in ("create", "update"):
                return "ERROR — operation must be create | update."
            if otype not in ("measure", "column", "relationship", "calculation_group"):
                return (
                    "ERROR — object_type must be measure | column | relationship | "
                    "calculation_group."
                )
            if operation == "update" and otype == "calculation_group":
                return (
                    "ERROR — calculation_group supports create only (in-place update "
                    "isn't wired). Propose a fresh group or delete-and-recreate."
                )
            if not name:
                return "ERROR — name is required."
            if not reason:
                return "ERROR — a `reason` is required (why is this edit needed?)."
            definition_value = tool_input.get("definition")
            if otype in ("relationship", "calculation_group"):
                definition_value = _coerce_json_object(definition_value)
                if definition_value is None:
                    return (
                        f"ERROR — `definition` for a {otype} must be an object "
                        "(or a JSON string encoding one)."
                    )
            if operation == "create" and otype == "calculation_group":
                if not isinstance(definition_value, dict):
                    return (
                        "ERROR — a calculation_group `definition` must be an object with "
                        "a non-empty `calculationItems` list, each item having a `name` "
                        "and a DAX `expression` (e.g. SELECTEDMEASURE())."
                    )
                _items = definition_value.get("calculationItems")
                if not isinstance(_items, list) or not any(
                    isinstance(it, dict) and (it.get("name") and it.get("expression"))
                    for it in _items
                ):
                    return (
                        "ERROR — a calculation_group `definition` must be an object with "
                        "a non-empty `calculationItems` list, each item having a `name` "
                        "and a DAX `expression` (e.g. SELECTEDMEASURE())."
                    )
            from model.model_edit_guard import validate_home_table

            table = (tool_input.get("table") or "").strip()
            _col_reg = _load_column_registry()
            _mea_reg = _load_measure_registry()
            _unverified: list[str] = []
            if not _col_reg and not _mea_reg:
                _unverified.append("registry empty")
            table_err = validate_home_table(otype, table, _col_reg, _mea_reg)
            if table_err:
                return table_err
            if operation == "update":
                _reg = _mea_reg if otype == "measure" else _col_reg
                _reg_keys = {k for k in (_reg or {}) if k != "_meta"}
                if otype in ("measure", "column") and _reg_keys:
                    if f"{table}.{name}" not in _reg_keys:
                        return (
                            f"ERROR — cannot update {otype} '{name}' on table "
                            f"'{table}': it does not exist in the connected model. Use "
                            'operation="create" to add it, or fix the name/table.'
                        )
                elif otype == "relationship":
                    _unverified.append("relationship existence not checked")
                else:
                    _unverified.append("existence not verified (registry empty)")
            if otype in ("measure", "column"):
                from model.dax_preflight import validate_dax_references

                pending_names = [
                    p.get("name", "")
                    for p in _mes.list_pending()
                    if p.get("object_type") == "measure"
                ]
                dax_err = validate_dax_references(
                    tool_input.get("definition", ""),
                    _col_reg,
                    _mea_reg,
                    extra_measures=pending_names,
                )
                if dax_err:
                    return dax_err
                if not DAX_LIVE_VALIDATE:
                    _unverified.append("live DAX validation disabled")
                elif not _active_models():
                    _unverified.append("no model connected for live validation")
                if DAX_LIVE_VALIDATE:
                    from model.dax_live_validate import validate_dax_live

                    pending_measures = [
                        (p.get("table", ""), p.get("name", ""), p.get("definition", ""))
                        for p in _mes.list_pending()
                        if p.get("object_type") == "measure"
                    ]
                    live_err = validate_dax_live(
                        (
                            f"powerbi://api.powerbi.com/v1.0/myorg/{_active_workspace()}"
                            if _target_model
                            else ""
                        ),
                        _target_model,
                        otype,
                        table,
                        name,
                        tool_input.get("definition", ""),
                        pending_measures=pending_measures,
                    )
                    if live_err:
                        return str(live_err)
            _mes.add_pending(
                {
                    "operation": operation,
                    "object_type": otype,
                    "name": name,
                    "table": table,
                    "definition": (
                        definition_value if definition_value is not None else ""
                    ),
                    "reason": reason,
                    "format_string": tool_input.get("format_string", ""),
                    "display_folder": tool_input.get("display_folder", ""),
                    # Pin the target resolved by _resolve_target_model(). Without
                    # this the approval panel had to guess, and guessed
                    # FABRIC_MODELS[0] -- the exact "acting on the wrong model"
                    # failure _resolve_target_model() exists to prevent.
                    "workspace": _active_workspace(),
                    "model": _target_model,
                }
            )
            try:
                from db.database import audit as _audit

                _audit(
                    f"model.propose_{operation}",
                    target=f"{otype}:{name}",
                    details=reason,
                )
            except Exception:
                pass
            _note = ""
            if _unverified:
                _note = (
                    " NOTE: references NOT verified ("
                    + "; ".join(_unverified)
                    + ") - will only be checked at apply time."
                )
            _verb = "update to" if operation == "update" else "new"
            _perm = (
                " This is a PERMANENT change to an existing object (no auto-revert)."
                if operation == "update"
                else ""
            )
            return (
                f"Proposed {_verb} {otype} '{name}' — QUEUED for user approval. It is NOT "
                "applied yet; the user must click Approve in the 'Pending model edits' "
                "panel before it touches the live model. Continue with any other "
                "proposals or explanation." + _perm + _note
            )
        except Exception as e:
            return f"ERROR — propose_model_edit failed: {type(e).__name__}: {e}"

    if tool_name == "pbi_layout_compute":
        try:
            from report.layout_engine import compute_layout

            return json.dumps(compute_layout(tool_input), indent=2)
        except Exception as e:
            return f"ERROR — pbi_layout_compute failed: {type(e).__name__}: {e}"

    if tool_name == "pbi_dashboard_layout":
        try:
            from report.dashboard_layout import compute_dashboard_layout

            return json.dumps(compute_dashboard_layout(tool_input), indent=2)
        except Exception as e:
            return f"ERROR — pbi_dashboard_layout failed: {type(e).__name__}: {e}"

    if tool_name == "pbi_report_resize_page":
        try:
            from report.dashboard_layout import plan_page_resize
            from report.visual_sizing import (
                DECORATIVE_VISUAL_TYPES,
                classify_visual,
                estimate_min_size,
                sizing_spec_from_pbir_visual,
            )

            try:
                target_w = float(tool_input["width"])
                target_h = float(tool_input["height"])
            except (KeyError, TypeError, ValueError):
                return (
                    "ERROR — pbi_report_resize_page requires numeric "
                    "width and height"
                )
            if not (
                REPORT_LAYOUT["canvas_min_w"]
                <= target_w
                <= REPORT_LAYOUT["canvas_max_w"]
            ):
                return (
                    f"ERROR — width must be between {REPORT_LAYOUT['canvas_min_w']} "
                    f"and {REPORT_LAYOUT['canvas_max_w']}px (got {int(target_w)})"
                )
            if not (
                REPORT_LAYOUT["canvas_min_h"]
                <= target_h
                <= REPORT_LAYOUT["canvas_max_h"]
            ):
                return (
                    f"ERROR — height must be between {REPORT_LAYOUT['canvas_min_h']} "
                    f"and {REPORT_LAYOUT['canvas_max_h']}px (got {int(target_h)})"
                )

            report_path = tool_input.get("report_path", "")
            page = tool_input.get("page", "")
            page_dirs = glob.glob(f"{report_path}/**/pages/{page}", recursive=True)
            if not page_dirs:
                return f"ERROR — page '{page}' not found under {report_path}"
            page_dir = page_dirs[0]
            page_json_path = os.path.join(page_dir, "page.json")
            if not os.path.isfile(page_json_path):
                return f"ERROR — page.json not found for page '{page}'"
            try:
                with open(page_json_path, encoding="utf-8") as f:
                    page_data = json.load(f)
            except Exception as e:
                return f"ERROR — could not read page.json: {e}"

            page_w = float(page_data.get("width", REPORT_LAYOUT["canvas_preferred_w"]))
            page_h = float(page_data.get("height", REPORT_LAYOUT["canvas_preferred_h"]))

            visuals_dir = os.path.join(page_dir, "visuals")
            visual_records: list[tuple[str, str, dict]] = []
            plan_input: list[dict] = []
            if os.path.isdir(visuals_dir):
                for v_name in sorted(os.listdir(visuals_dir)):
                    v_path = os.path.join(visuals_dir, v_name, "visual.json")
                    if not os.path.isfile(v_path):
                        continue
                    try:
                        with open(v_path, encoding="utf-8") as f:
                            v_data = json.load(f)
                    except Exception as e:
                        return (
                            f"ERROR — could not read visual.json for "
                            f"'{v_name}': {e}"
                        )
                    pos = v_data.get("position", {})
                    vtype = v_data.get("visual", {}).get("visualType", "")
                    vclass = classify_visual({"type": vtype})
                    exempt = vtype in DECORATIVE_VISUAL_TYPES
                    min_w, min_h = estimate_min_size(
                        sizing_spec_from_pbir_visual(v_data), vclass
                    )
                    visual_records.append((v_name, v_path, v_data))
                    plan_input.append(
                        {
                            "name": v_name,
                            "x": pos.get("x", 0),
                            "y": pos.get("y", 0),
                            "width": pos.get("width", 0),
                            "height": pos.get("height", 0),
                            "min_w": min_w,
                            "min_h": min_h,
                            "exempt": exempt,
                        }
                    )

            plan = plan_page_resize(page_w, page_h, target_w, target_h, plan_input)
            if "error" in plan:
                return f"ERROR — {plan['error']}"

            # Validated — commit. page.json first, then each adjusted visual.
            page_data["width"] = plan["width"]
            page_data["height"] = plan["height"]
            _atomic_write_json(page_json_path, page_data, indent=2)
            _trace_file_after_write(page_json_path, "pbi_report_resize_page:page")

            for v_name, v_path, v_data in visual_records:
                adj = plan["adjustments"].get(v_name)
                if not adj:
                    continue
                v_data.setdefault("position", {})
                v_data["position"]["width"] = adj["width"]
                v_data["position"]["height"] = adj["height"]
                _atomic_write_json(v_path, v_data, indent=2)
                _trace_file_after_write(v_path, "pbi_report_resize_page:visual")

            n_adj = len(plan["adjustments"])
            if n_adj:
                _adj_list = ", ".join(sorted(plan["adjustments"]))
                return (
                    f"Resized page '{page}' to {int(plan['width'])}x"
                    f"{int(plan['height'])}. {n_adj} visual(s) adjusted to "
                    f"fit: {_adj_list}"
                )
            return (
                f"Resized page '{page}' to {int(plan['width'])}x"
                f"{int(plan['height'])}. No visuals needed adjustment."
            )
        except Exception as e:
            return f"ERROR — pbi_report_resize_page failed: {type(e).__name__}: {e}"

    if tool_name == "pbi_add_svg_measure":
        try:
            from report.visual_format import add_extension_measures

            expr = tool_input["expression"]
            if "%23" not in expr and "#" in expr:
                return (
                    "ERROR — the SVG expression contains a raw '#': hex colors inside a "
                    "data URI must be escaped as %23RRGGBB or the image renders blank. "
                    "Fix the expression and retry."
                )
            _err, _note = _validate_format_field(tool_input["entity"], None, tool_input)
            if _err:
                return _err
            out = add_extension_measures(
                tool_input["report_path"],
                tool_input["entity"],
                [
                    {
                        "name": tool_input["name"],
                        "expression": expr,
                        "dataType": "Text",
                        "dataCategory": "ImageUrl",
                    }
                ],
            )
            _snapshot_report(tool_input["report_path"], source="after-svg-measure")
            if _note and "ERROR" not in out:
                out += _note
            return out
        except Exception as e:
            return f"ERROR — pbi_add_svg_measure failed: {type(e).__name__}: {e}"

    if tool_name == "pbi_design_judge":
        try:
            from report.design_judge import judge_page

            _findings = None
            try:
                _audit = _audit_dashboard(tool_input["report_path"])
                _pg = (_audit.get("pages") or {}).get(tool_input["page"]) or {}
                _findings = (_pg.get("errors") or []) + (_pg.get("warnings") or [])
            except Exception:
                pass
            return json.dumps(
                judge_page(
                    tool_input["report_path"],
                    tool_input["page"],
                    audit_findings=_findings,
                    round_num=int(tool_input.get("round", 1) or 1),
                    prev_overall=tool_input.get("prev_overall"),
                ),
                indent=2,
                default=str,
            )
        except Exception as e:
            return f"ERROR — pbi_design_judge failed: {type(e).__name__}: {e}"

    if tool_name == "pbi_card_set_image":
        try:
            from report.visual_format import set_card_image

            if bool(tool_input.get("from_extension", True)):
                _err, _note = _validate_extension_measure_ref(
                    tool_input["report_path"],
                    tool_input["entity"],
                    tool_input["measure"],
                    tool_input,
                )
            else:
                _err, _note = _validate_format_field(
                    tool_input["entity"], tool_input["measure"], tool_input
                )
            if _err:
                return _err
            _out = set_card_image(
                tool_input["report_path"],
                tool_input["visual_name"],
                entity=tool_input["entity"],
                measure=tool_input["measure"],
                position=tool_input.get("position", "Bottom"),
                from_extension=bool(tool_input.get("from_extension", True)),
                page=tool_input.get("page"),
            )
            if _note and "ERROR" not in _out:
                _out += _note
            return _out
        except Exception as e:
            return f"ERROR — pbi_card_set_image failed: {type(e).__name__}: {e}"

    if tool_name == "pbi_model_quality":
        try:
            from model.model_quality import find_model_dir, score_model

            mdir = find_model_dir(tool_input.get("path", ""))
            if not mdir:
                return f"ERROR — no .SemanticModel found from path {tool_input.get('path', '')!r}."
            return json.dumps(score_model(mdir), indent=2)
        except Exception as e:
            return f"ERROR — pbi_model_quality failed: {type(e).__name__}: {e}"

    if tool_name == "pbi_tmdl_doctor":
        try:
            from model.tmdl_doctor import diagnose_and_fix

            return json.dumps(
                diagnose_and_fix(
                    tool_input.get("path", ""), apply=bool(tool_input.get("apply"))
                ),
                indent=2,
            )
        except Exception as e:
            return f"ERROR — pbi_tmdl_doctor failed: {type(e).__name__}: {e}"

    if tool_name == "pbi_report_review":
        try:
            from report import report_review as _rr

            report_path = tool_input.get("report_path", "")
            analysis = _rr.analyze_dashboard(report_path)
            if analysis.get("error"):
                return f"ERROR — {analysis['error']}"
            change_request = (tool_input.get("change_request") or "").strip()
            result = {"analysis": analysis}
            if change_request:
                result["proposal"] = _rr.propose_changes(analysis, change_request)
            return json.dumps(result, indent=2, default=str)
        except Exception as e:
            return f"ERROR — pbi_report_review failed: {type(e).__name__}: {e}"

    if tool_name == "pbi_dax_page":
        try:
            from dax.dax_spill import page_csv

            return page_csv(
                tool_input["csv_path"],
                int(tool_input.get("offset", 0)),
                int(tool_input.get("limit", 50)),
            )
        except Exception as e:
            return f"ERROR — pbi_dax_page failed: {type(e).__name__}: {e}"

    if tool_name == "pbi_model_bpa":
        try:
            from report.bpa_lint import lint_model

            return json.dumps(
                lint_model(
                    tool_input["model"],
                    workspace=tool_input.get("workspace", ""),
                    server=tool_input.get("server", ""),
                ),
                indent=2,
            )
        except Exception as e:
            return f"ERROR — pbi_model_bpa failed: {type(e).__name__}: {e}"

    if tool_name == "pbi_model_test":
        try:
            from model.model_tests import run_spec

            return json.dumps(run_spec(tool_input["spec_path"]), indent=2, default=str)
        except Exception as e:
            return f"ERROR — pbi_model_test failed: {type(e).__name__}: {e}"

    if tool_name == "pbi_generate_docs":
        try:
            from model.model_docs import DOC_FILENAMES, generate_doc
            from model.model_quality import find_model_dir

            mdir = find_model_dir(tool_input.get("path", "")) or tool_input.get(
                "path", ""
            )
            dtype = tool_input.get("doc_type") or "data_dictionary"
            md = generate_doc(mdir, dtype)
            if md.startswith("ERROR"):
                return md
            if tool_input.get("save"):
                fname = DOC_FILENAMES.get(dtype, "model_docs.md")
                out = os.path.join(mdir, fname)
                with open(out, "w", encoding="utf-8") as f:
                    f.write(md)
                return f"Wrote {dtype} to {out}\n\n{md[:1500]}\n…(truncated; full file saved)"
            return md
        except Exception as e:
            return f"ERROR — pbi_generate_docs failed: {type(e).__name__}: {e}"

    if tool_name == "pbi_dax_analyze":
        try:
            from model import dax_analyze as _da

            _expr = tool_input.get("expression", "")
            _mode = (tool_input.get("mode") or "explain").lower()
            _col_reg = _load_column_registry()
            _mea_reg = _load_measure_registry()
            if _mode == "optimize":
                return _da.optimize_dax(_expr)
            if _mode == "debug":
                if not tool_input.get("table"):
                    return (
                        "ERROR — pbi_dax_analyze mode='debug' requires 'table' "
                        "(the expression's home table) so the live probe can DEFINE "
                        "it; without it the probe silently ran against an empty "
                        "table string. Re-call with 'table' set."
                    )
                _models = _active_models()
                return _da.debug_dax(
                    _expr,
                    _col_reg,
                    _mea_reg,
                    server=(
                        f"powerbi://api.powerbi.com/v1.0/myorg/{_active_workspace()}"
                        if _models
                        else ""
                    ),
                    database=_models[0] if _models else "",
                    table=tool_input.get("table", ""),
                    otype=tool_input.get("object_type") or "measure",
                )
            return _da.explain_dax(_expr, _col_reg, _mea_reg)
        except Exception as e:
            return f"ERROR — pbi_dax_analyze failed: {type(e).__name__}: {e}"

    if tool_name == "pbi_export_model":
        try:
            from model.model_export import export_excel
            from model.model_quality import find_model_dir

            mdir = find_model_dir(tool_input.get("path", "")) or tool_input.get(
                "path", ""
            )
            out = export_excel(mdir)
            return f"Exported model to Excel: {out}"
        except Exception as e:
            return f"ERROR — pbi_export_model failed: {type(e).__name__}: {e}"

    _guard_err = _guard_tool_inputs(tool_name, tool_input)
    if _guard_err:
        _log_hallucination([], "input_guard_rejected", tool_input)
        return _guard_err

    if tool_name in (
        "pbi_paginated_create",
        "pbi_paginated_get",
        "pbi_paginated_set",
        "pbi_paginated_validate",
        "pbi_paginated_delete",
        "pbi_paginated_list",
    ):
        try:
            from config import (
                FABRIC_WORKSPACE as _DEF_WS,
            )
            from config import (
                PAGINATED_DESIGN,
                PBI_PAGINATED_CATALOGS,
            )
            from report import rdl_builder

            p = tool_input.get("path", "")
            # Paginated reports live under PROJECTS_DIR/<project>/paginated/<Report>.rdl
            from report import paginated_store as _pstore

            def _active_project() -> str:
                try:
                    return state().get("active_project_name", "") or ""
                except Exception:
                    return ""

            def _dest(raw: str):
                return _pstore.resolve_dest(raw, _active_project())

            def _existing(raw: str):
                return _pstore.find_existing(raw, _active_project())

            def _all_rdls():
                return _pstore.all_rdls()

            if tool_name == "pbi_paginated_list":
                rdls = _all_rdls()
                # Absolute paths, so the next call can copy one verbatim. A
                # relative path invites the model to rebuild it by hand, which
                # is how "temp-fabricbi" became "temp\fabricbi".
                return "\n".join(rdls) if rdls else "No paginated (.rdl) reports found."

            if tool_name == "pbi_paginated_get":
                src = _existing(p)
                if src is None:
                    known = _all_rdls()
                    hint = (
                        "\nExisting .rdl files (pass one of these verbatim):\n  "
                        + "\n  ".join(known)
                        if known
                        else "\nNo .rdl files exist in the project store."
                    )
                    return f"ERROR — no .rdl at {p!r}.{hint}"
                return src.read_text(encoding="utf-8")

            if tool_name == "pbi_paginated_delete":
                if not p.lower().endswith(".rdl"):
                    return "ERROR — refusing to delete a non-.rdl file."
                victim = _existing(p)
                if victim is None:
                    return f"ERROR — no .rdl at {p!r} in the project store."
                os.remove(victim)
                return f"Deleted {victim}"

            if tool_name == "pbi_paginated_validate":
                src = _existing(p)
                if src is None:
                    return f"ERROR — no .rdl at {p!r} in the project store."
                issues = rdl_builder.validate_rdl(src.read_text(encoding="utf-8"))
                return (
                    "ok — structurally valid."
                    if not issues
                    else ("INVALID:\n- " + "\n- ".join(issues))
                )

            if tool_name == "pbi_paginated_set":
                xml = tool_input.get("xml", "")
                issues = rdl_builder.validate_rdl(xml)
                if issues:
                    return (
                        "REFUSED — RDL is structurally invalid, not saved:\n- "
                        + "\n- ".join(issues)
                    )
                dest = _existing(p)
                if dest is None:
                    dest, err = _dest(p)
                    if err:
                        return err
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(xml, encoding="utf-8")
                return f"Saved valid RDL to {dest}"

            ws = tool_input.get("workspace") or _DEF_WS
            if not ws:
                return "ERROR — no workspace given and FABRIC_WORKSPACE not set."
            _ds_name = tool_input.get("dataset", "")
            _catalog = next(
                (
                    v
                    for k, v in PBI_PAGINATED_CATALOGS.items()
                    if k.lower() == _ds_name.lower()
                ),
                "",
            )
            # Validate DAX query before assembling report
            _dax = tool_input.get("dax", "")
            _cols_live: list[str] = []
            if DAX_LIVE_VALIDATE and _dax.strip():
                from fabric import pbi_xmla as _xmla

                _cols_live, _dax_err = _xmla.dax_result_columns(
                    f"powerbi://api.powerbi.com/v1.0/myorg/{ws}", _ds_name, _dax
                )
                if _dax_err:
                    return (
                        "REFUSED — the report's DAX query does not run against "
                        f"'{_ds_name}', so no .rdl was written:\n{_dax_err}\n\n"
                        "Check the table and column names with pbi_model_inspect, "
                        "then call pbi_paginated_create again with a corrected "
                        "`dax`."
                    )

            xml = rdl_builder.build_rdl(
                title=tool_input.get("title", "Report"),
                subtitle=tool_input.get("subtitle", ""),
                workspace=ws,
                dataset=_ds_name,
                catalog=_catalog,
                dax=tool_input.get("dax", ""),
                layout=tool_input.get("layout", "table"),
                columns=tool_input.get("columns") or [],
                total_row=bool(tool_input.get("total_row", False)),
                rows_field=tool_input.get("rows_field", ""),
                cols_field=tool_input.get("cols_field", ""),
                value_field=tool_input.get("value_field", ""),
                landscape=bool(tool_input.get("landscape", False)),
                summary_cards=tool_input.get("summary_cards") or [],
                group_field=tool_input.get("group_field", ""),
                page_break_per_group=bool(
                    tool_input.get("page_break_per_group", False)
                ),
                design=PAGINATED_DESIGN,
            )
            issues = rdl_builder.validate_rdl(xml)
            if issues:
                return "ERROR — generated RDL failed validation:\n- " + "\n- ".join(
                    issues
                )
            # The query ran, so its real column names are known: check the
            # report reads columns it actually returns. A mismatch renders as an
            # empty column rather than an error, which is harder to notice than
            # a failure and is why it went unreported.
            if _cols_live:
                _missing = rdl_builder.unmatched_data_fields(xml, _cols_live)
                if _missing:
                    return (
                        "REFUSED — the report reads columns the query does not "
                        f"return, so no .rdl was written: {', '.join(_missing)}.\n"
                        f"The query returns: {', '.join(_cols_live)}.\n"
                        "Use those names in `columns`/`summary_cards`, or change "
                        "the `dax` to return the ones you want."
                    )
            dest, err = _dest(p or tool_input.get("title", "Report"))
            if err:
                return err
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(xml, encoding="utf-8")
            return (
                f"Created paginated report at {dest} "
                f"({tool_input.get('layout', 'table')} layout). Edit with "
                f"pbi_paginated_get/set; open in Power BI Report Builder to render."
            )
        except Exception as e:
            return f"ERROR — {tool_name} failed: {type(e).__name__}: {e}"

    if tool_name == "pbi_memory_search":
        from db import graph_memory

        return graph_memory.search(tool_input.get("term", ""))

    if tool_name == "pbi_reports_list":
        from report.report_catalog import format_catalog, list_reports

        return format_catalog(list_reports(tool_input.get("root", "")))

    if tool_name == "pbi_report_inspect":
        from report.legacy_reader import format_bindings, format_summary, read

        _rp = tool_input.get("report_path", "")
        try:
            _legacy = read(_rp)
            if _legacy is None:
                # Either a real PBIR report (use the PBIR tools) or a bad path.
                if not os.path.isdir(_rp):
                    return f"ERROR — no such report folder: {_rp}"
                return (
                    f"{_rp} is in PBIR format (or has no report.json) — use "
                    f"pbi_report_info / pbi_visual_list / pbi_visual_bindings "
                    f"for it. This tool is for PBIR-Legacy reports."
                )
            if tool_input.get("bindings"):
                return format_bindings(_legacy)
            return format_summary(_legacy)
        except Exception as e:
            return f"ERROR — report inspect failed: {type(e).__name__}: {e}"

    if tool_name == "pbi_report_open":
        # One step for "show me what's in <report>": resolve the name against
        # the workspace, download it if it isn't already local, then read it
        # with whichever reader its format needs. Previously the assistant had
        # to chain fetch -> guess the path -> pick a reader, and usually
        # answered "no reports found" from the LOCAL scan instead.
        from pathlib import Path as _P

        from fabric.report_fetch import (
            FetchError,
            fetch_report,
            format_result,
            safe_name,
            temp_root,
        )

        _name = (tool_input.get("report") or "").strip()
        if not _name:
            return "ERROR — `report` (the published report's name) is required."

        _refetch = bool(tool_input.get("refresh"))
        _root = None
        _notes: list[str] = []
        try:
            _root = temp_root() / safe_name(_name)
        except Exception:
            _root = None
        _rdir = (_root / f"{safe_name(_name)}.Report") if _root else None

        if _refetch or not (_rdir and _rdir.is_dir()):
            try:
                _res = fetch_report(report_name=_name, include_pbix=False)
            except FetchError as e:
                return (
                    f"FAILED — {e}\nNothing was downloaded; do not describe the "
                    f'report. Use pbi_workspace_items kind="reports" to see the '
                    f"names that exist."
                )
            except Exception as e:
                return (
                    f"FAILED — could not open {_name!r}: {type(e).__name__}: {e}\n"
                    f"Nothing was downloaded; do not describe the report."
                )
            if not _res.ok or not _res.path:
                return format_result(_res)
            _rdir = _P(_res.path) / f"{safe_name(_res.name)}.Report"
            _notes = list(_res.notes)
            _name = _res.name
        else:
            _notes.append("already downloaded; pass refresh=true to re-fetch.")

        _out = [f"REPORT: {_name}", f"LOCAL PATH: {_rdir}"]
        if _notes:
            _out += [f"  note: {n}" for n in _notes]

        # PBIR-Legacy reports only open with the legacy reader; real PBIR ones
        # go through the scan-backed tools.
        format_summary = None
        format_bindings = None
        try:
            from report.legacy_reader import format_bindings, format_summary, read

            _legacy = read(str(_rdir))
        except Exception as e:
            _legacy = None
            _out.append(f"  note: legacy reader failed: {type(e).__name__}: {e}")

        if _legacy is not None and format_summary is not None:
            _out.append("FORMAT: PBIR-Legacy (read with pbi_report_inspect)")
            _out.append(format_summary(_legacy))
            if tool_input.get("bindings") and format_bindings is not None:
                _out.append(format_bindings(_legacy))
        else:
            _out.append("FORMAT: PBIR")
            _out.append(_summarise_pbir(str(_rdir)))
        _out.append("ACTIVE_REPORT_PATH=" + str(_rdir))
        return "\n".join(_out)

    if tool_name == "pbi_report_fetch":
        from fabric.report_fetch import FetchError, fetch_report, format_result

        try:
            return format_result(
                fetch_report(
                    report_name=tool_input.get("report", ""),
                    include_pbix=bool(tool_input.get("include_pbix")),
                )
            )
        except FetchError as e:
            return f"FAILED — {e}\nNothing was downloaded; do not describe the report."
        except Exception as e:
            return (
                f"FAILED — report fetch errored: {type(e).__name__}: {e}\n"
                f"Nothing was downloaded; do not describe the report."
            )

    if tool_name in ("pbi_model_inspect", "pbi_dax_query"):
        # Live-model access over XMLA with the service principal. This is the
        # supported path: the powerbi_modeling MCP server cannot authenticate
        # as an SP in this tenant (its ConnectFabric 401s at cluster
        # resolution, and it refuses ADOMD's native `User ID=app:...` form),
        # so its table/column/measure/dax tools are unusable here.
        from fabric import pbi_xmla

        _ws = (
            tool_input.get("workspace")
            or state().get("fb_selected_ws")
            or _active_workspace()
        )
        if not _ws:
            return "ERROR — no workspace: connect on Project Management, or pass `workspace`."
        _model, _model_err = _resolve_target_model(tool_input.get("model"))
        if _model_err:
            return _model_err
        _server = f"powerbi://api.powerbi.com/v1.0/myorg/{_ws}"

        if tool_name == "pbi_dax_query":
            _dax = (tool_input.get("dax") or "").strip()
            if not _dax:
                return "ERROR — `dax` is required."
            if not re.match(r"^\s*(define|evaluate)\b", _dax, re.IGNORECASE):
                return (
                    "ERROR — a DAX query must start with EVALUATE (or DEFINE ... "
                    "EVALUATE). To analyse an expression instead, use pbi_dax_analyze."
                )
            try:
                return f"MODEL: {_ws}/{_model}\n" + pbi_xmla.run_dax(
                    _server, _model, _dax
                )
            except Exception as e:
                return f"ERROR — DAX query failed: {type(e).__name__}: {e}"

        try:
            meta = pbi_xmla.fetch_model_metadata(_server, _model)
        except Exception as e:
            return f"ERROR — could not read model {_model!r}: {type(e).__name__}: {e}"

        _section = (tool_input.get("section") or "all").lower()
        out = [f"MODEL: {_ws}/{_model}"]
        _tables = meta.get("tables") or []
        _rc = meta.get("row_counts") or {}
        if _section in ("all", "tables"):
            out.append(f"\nTABLES ({len(_tables)}):")
            for t in _tables:
                _n = _rc.get(t)
                out.append(f"  {t}" + (f"  [{_n} rows]" if _n is not None else ""))
        if _section in ("all", "measures"):
            _mbt = meta.get("measures_by_table") or {}
            _total = sum(len(v) for v in _mbt.values())
            out.append(
                f"\nMEASURES ({_total}) — use exact Table[Measure] when binding:"
            )
            for t, ms in sorted(_mbt.items()):
                for m in ms:
                    out.append(f"  {t}[{m}]")
            for err in meta.get("measure_errors") or []:
                out.append(f"  ! {err}")
        if _section in ("all", "columns"):
            _cbt = meta.get("columns_by_table") or {}
            out.append("\nCOLUMNS:")
            for t, cols in sorted(_cbt.items()):
                _names = ", ".join(str(c[0]) for c in cols)
                out.append(f"  {t}: {_names}")
        if _section in ("all", "relationships"):
            _rels = meta.get("relationships") or []
            out.append(f"\nRELATIONSHIPS ({len(_rels)}):")
            for r in _rels:
                _flag = "" if r.get("is_active") else "  (INACTIVE)"
                out.append(f"  {pbi_xmla.relationship_label(r, '->')}{_flag}")
        # Built from the same metadata rather than from the text above, so the
        # picture cannot drift from what the model actually reports.
        if _section in ("all", "tables", "relationships"):
            from fabric.model_diagram import build_model_diagram

            _log_visual("model_diagram", build_model_diagram(f"{_ws}/{_model}", meta))
        return "\n".join(out)

    if tool_name == "pbi_workspace_items":
        from fabric.fabric_browse import browse

        try:
            return browse(
                kind=tool_input.get("kind") or "semantic_models",
                # Default to the workspace the session is connected to, so the
                # assistant does not have to know the name to answer "what else
                # is in here?". session_state is the live value the connection
                # chip writes; the config constant is only the .env fallback.
                workspace=(
                    tool_input.get("workspace")
                    or state().get("fb_selected_ws")
                    or _ws_ctx.FABRIC_WORKSPACE
                ),
                name_filter=tool_input.get("name_filter") or "",
            )
        except Exception as e:
            return f"ERROR — workspace listing failed: {type(e).__name__}: {e}"

    if tool_name == "pbi_impact_analysis":
        from fabric.impact import run_impact_analysis

        _removed = tool_input.get("removed_fields") or []
        if isinstance(_removed, str):
            _removed = [f.strip() for f in _removed.split(",") if f.strip()]
        try:
            return run_impact_analysis(
                model=tool_input.get("model", ""),
                removed_fields=list(_removed),
                workspace=tool_input.get("workspace", ""),
                refresh=bool(tool_input.get("refresh")),
            )
        except Exception as e:
            # A crashed safety net must never read as "all clear".
            return (
                f"ERROR — impact analysis failed: {type(e).__name__}: {e}. "
                f"Treat the downstream impact as UNKNOWN, not as none."
            )

    if tool_name == "pbi_user_access":
        from auth.access_policy import access_report

        # Both the role check and the credential choice live in
        # auth.access_policy, which every entry path shares. The lookup now
        # runs on the caller's own delegated token, so a non-admin asking
        # about a colleague is refused here AND would be refused by Entra --
        # this branch cannot be the only thing standing in the way.
        try:
            result = access_report(
                tool_input.get("userId", ""),
                artifact_types=tool_input.get("artifactTypes", "") or "",
            )
        except Exception as e:
            return f"ERROR — user access lookup failed: {type(e).__name__}: {e}"
        error = str(result.get("error") or "")
        if error.startswith("REFUSED"):
            return error
        return json.dumps(result, indent=2, ensure_ascii=False)

    if tool_name == "pbi_lineage_map":
        from fabric.impact import run_lineage_map

        try:
            return run_lineage_map(
                model=tool_input.get("model", ""),
                workspace=tool_input.get("workspace", ""),
                refresh=bool(tool_input.get("refresh")),
            )
        except Exception as e:
            return f"ERROR — lineage map failed: {type(e).__name__}: {e}"

    report_path = tool_input.get("report_path", "")
    path_args = ["--path", report_path] if report_path else []

    if tool_name in _VISUAL_EDIT_TOOLS:
        _vn = tool_input.get("visual_name", "")
        _pg = tool_input.get("page", "")
        if _vn and _pg and report_path:
            _existing = _list_page_visual_names(report_path, _pg)
            if _existing and _vn not in _existing:
                _log_hallucination(
                    [f"visual_name={_vn}"],
                    "visual_not_on_page",
                    tool_input,
                )
                return (
                    f"ERROR — refused to {tool_name}: visual '{_vn}' is not "
                    f"present on page '{_pg}'. Current visuals on this page: "
                    f"{_existing}. Call pbi_visual_list FIRST to see "
                    f"actual names, then retry. (FORCE-LIST-BEFORE-EDIT rule.)"
                )

    if tool_name == "pbi_visual_bind":
        _combo_err = _validate_visual_bind_combo(tool_input)
        if _combo_err:
            _log_hallucination(
                [f"empty_bind on {tool_input.get('visual_name', '?')}"],
                "empty_bind",
                tool_input,
            )
            return _combo_err

    try:
        if tool_name == "pbi_report_create":
            cmd = [
                *PBI_CLI,
                "report",
                "--no-sync",
                "create",
                tool_input["path"],
                "--name",
                tool_input["name"],
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
            output = ((result.stdout or "") + (result.stderr or "")).strip()
            _err = _pbi_exit_error(tool_name, result, output)
            if _err:
                return _err

            pbir_files = glob.glob(f"{tool_input['path']}/**/*.pbir", recursive=True)
            if pbir_files:
                workspace = _active_workspace()
                model_name = tool_input["model_name"]
                dataset_id = tool_input["dataset_id"]
                conn_str = (
                    f"Data Source=powerbi://api.powerbi.com/v1.0/myorg/{workspace};"
                    f'initial catalog="{model_name}";'
                    f"access mode=readonly;"
                    f"integrated security=ClaimsToken;"
                    f"semanticmodelid={dataset_id}"
                )
                definition = {
                    "$schema": (
                        "https://developer.microsoft.com/json-schemas/fabric/"
                        "item/report/definitionProperties/2.0.0/schema.json"
                    ),
                    "version": "4.0",
                    "datasetReference": {
                        "byConnection": {
                            "connectionString": conn_str,
                        }
                    },
                }
                _atomic_write_json(pbir_files[0], definition, indent=2)
                _trace_file_after_write(
                    pbir_files[0], "pbi_report_create:definition.pbir"
                )
                output += f"\nFabric connection wired: {model_name} ({dataset_id})"

                pbip_files = glob.glob(f"{tool_input['path']}/*.pbip")
                for pbip_path in pbip_files:
                    try:
                        with open(pbip_path, encoding="utf-8") as _pf:
                            _pbip = json.load(_pf)
                        _ordered = {
                            "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
                            "version": _pbip.get("version", "1.0"),
                            "artifacts": _pbip.get("artifacts", []),
                            "settings": _pbip.get("settings", {}),
                        }
                        _atomic_write_json(pbip_path, _ordered, indent=2)
                        _trace_file_after_write(
                            pbip_path, "pbi_report_create:pbip-rewrite"
                        )
                    except Exception as _e:
                        output += f"\n[pbip-fix] failed: {_e}"
                        _build_trace(
                            "pbip_rewrite_error", path=pbip_path, error=str(_e)
                        )

                report_folders = glob.glob(
                    f"{tool_input['path']}/*.Report", recursive=False
                )
                if report_folders:
                    rpt_folder = report_folders[0]
                    try:
                        polish_msg = inject_theme(rpt_folder, palette=_active_palette())
                        output += f"\n{polish_msg}"
                    except Exception as e:
                        output += f"\n[polish] theme injection failed: {e}"
                    output += f"\nACTIVE_REPORT_PATH={rpt_folder.replace(chr(92), '/')}"
            return output or "Done."

        elif tool_name == "pbi_report_add_page":
            cmd = (
                [*PBI_CLI, "report", "--no-sync"]
                + path_args
                + [
                    "add-page",
                    "--display-name",
                    tool_input["display_name"],
                    "--name",
                    tool_input["name"],
                ]
            )
            result = subprocess.run(
                cmd,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
            output = ((result.stdout or "") + (result.stderr or "")).strip()
            _err = _pbi_exit_error(tool_name, result, output)
            if _err:
                return _err
            if report_path:
                for pjf in glob.glob(
                    f"{report_path}/**/pages/pages.json", recursive=True
                ):
                    try:
                        with open(pjf, encoding="utf-8") as f:
                            pages_data = json.load(f)
                        page_order = pages_data.get("pageOrder", [])
                        page_name = tool_input["name"]
                        if page_name not in page_order:
                            page_order.append(page_name)
                            pages_data["pageOrder"] = page_order
                            _atomic_write_json(pjf, pages_data, indent=2)
                            output += f" [pageOrder → {page_order}]"
                    except Exception:
                        pass

                page_name = str(tool_input.get("name") or "")
                display_name = str(tool_input.get("display_name") or page_name)
                for page_folder in glob.glob(
                    f"{report_path}/**/pages/{page_name}", recursive=True
                ):
                    try:
                        polish_msg = _apply_page_background_dynamic(
                            page_folder, display_name
                        )
                        output += f"\n{polish_msg}"
                    except Exception as e:
                        output += f"\n[polish] page background failed: {e}"
                    break
            return output or "Done."

        elif tool_name == "pbi_visual_add":
            _xywh_err = _require_xywh(tool_name, tool_input)
            if _xywh_err:
                return _xywh_err
            _snapped = False
            for _k in ("x", "y", "width", "height"):
                _sv = _snap_px(tool_input[_k])
                if float(tool_input[_k]) != float(_sv):
                    _snapped = True
                tool_input[_k] = _sv
            cmd = (
                [*PBI_CLI, "visual", "--no-sync"]
                + path_args
                + [
                    "add",
                    "--page",
                    tool_input["page"],
                    "--type",
                    tool_input["type"],
                    "--name",
                    tool_input["name"],
                    "--x",
                    str(tool_input["x"]),
                    "--y",
                    str(tool_input["y"]),
                    "--width",
                    str(tool_input["width"]),
                    "--height",
                    str(tool_input["height"]),
                ]
            )
            result = subprocess.run(
                cmd,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
            output = ((result.stdout or "") + (result.stderr or "")).strip()
            _err = _pbi_exit_error(tool_name, result, output)
            if _err:
                return _err
            if _snapped:
                output += f" [snapped to {REPORT_LAYOUT['grid_px']}px grid]"

            if report_path:
                for _vp in glob.glob(
                    f"{report_path}/**/visuals/{tool_input['name']}/visual.json",
                    recursive=True,
                ):
                    try:
                        with open(_vp, encoding="utf-8") as _vf:
                            _vd = json.load(_vf)
                        _old = _vd.get("$schema", "")
                        if "2.7.0" in _old:
                            _vd["$schema"] = _old.replace("2.7.0", "2.8.0")
                            _atomic_write_json(_vp, _vd, indent=2)
                            _trace_file_after_write(
                                _vp, "pbi_visual_add:schema-bump-2.8.0"
                            )
                        else:
                            _trace_file_after_write(_vp, "pbi_visual_add:initial")
                    except Exception as _e:
                        _build_trace(
                            "visual_add_schema_bump_error", path=_vp, error=str(_e)
                        )

            slicer_types = {
                "slicer",
                "list_slicer",
                "text_slicer",
                "advanced_slicer",
                "listSlicer",
                "textSlicer",
                "advancedSlicerVisual",
            }
            if tool_input.get("type") in slicer_types and report_path:
                from util.json_utils import fix_pbir_node as _fix_node

                matches = glob.glob(
                    f"{report_path}/**/visuals/{tool_input['name']}/visual.json",
                    recursive=True,
                )
                for vpath in matches:
                    try:
                        with open(vpath, encoding="utf-8") as f:
                            vdata = json.load(f)
                        _fix_node(vdata)
                        _atomic_write_json(vpath, vdata, indent=2)
                        _trace_file_after_write(vpath, "pbi_visual_add:slicer-fix")
                        output += " [slicer schema fixed]"
                    except Exception as _e:
                        _build_trace("slicer_fix_error", path=vpath, error=str(_e))

            if tool_input.get("type") in ("cardVisual", "card_visual") and report_path:
                try:
                    from report.dashboard_polish import THEME_JSON
                    from report.visual_format import style_kpi_card

                    _theme = _registered_theme(report_path) or THEME_JSON
                    accent = (
                        _theme.get("tableAccent")
                        or (_theme.get("dataColors") or [THEME_JSON["dataColors"][0]])[
                            0
                        ]
                    )
                    output += " " + style_kpi_card(
                        report_path,
                        tool_input["name"],
                        accent_color=accent,
                        page=tool_input.get("page"),
                    )
                except Exception as _e:
                    _build_trace(
                        "kpi_card_style_error",
                        name=tool_input.get("name"),
                        error=str(_e),
                    )
            output = (output or "Done.") + _overlap_note_for(tool_input, "name")
            return output

        elif tool_name == "pbi_visual_add_bound":
            _add_out = execute_pbi_tool(
                "pbi_visual_add",
                {
                    k: tool_input[k]
                    for k in (
                        "page",
                        "report_path",
                        "type",
                        "name",
                        "x",
                        "y",
                        "width",
                        "height",
                    )
                },
            )
            if isinstance(_add_out, str) and _add_out.startswith("ERROR"):
                return _add_out
            parts = [_add_out]
            common = {
                "visual_name": tool_input["name"],
                "page": tool_input["page"],
                "report_path": tool_input["report_path"],
            }
            _bindings = tool_input.get("bindings") or []
            _bind_fail_count = 0
            for b in _bindings:
                _bind_res = execute_pbi_tool("pbi_visual_bind", {**common, **(b or {})})
                parts.append(_bind_res)
                if isinstance(_bind_res, str) and _bind_res.startswith("ERROR"):
                    _bind_fail_count += 1
            if _bind_fail_count:
                parts.insert(
                    1,
                    f"⚠️ {_bind_fail_count} of {len(_bindings)} binding(s) FAILED — "
                    f"visual '{tool_input['name']}' was created but is only "
                    "partially bound. See the ERROR line(s) below for details.",
                )
            if tool_input.get("title"):
                parts.append(
                    execute_pbi_tool(
                        "pbi_visual_set_container",
                        {**common, "title": tool_input["title"]},
                    )
                )
            _sort_note = _apply_default_sort(
                tool_input["report_path"], tool_input["page"], tool_input["name"]
            )
            if _sort_note:
                parts.append(_sort_note.strip())
            _alt_note = _apply_alt_text(
                tool_input["report_path"],
                tool_input["page"],
                tool_input["name"],
                tool_input.get("title", ""),
                tool_input.get("bindings") or [],
            )
            if _alt_note:
                parts.append(_alt_note.strip())

            # (Auto-apply Top-N and Non-Blank filters have been disabled due to PBIR schema corruption)
            return "\n".join(parts)

        elif tool_name == "pbi_refresh_registry":
            models = tuple(
                m.strip()
                for m in (tool_input.get("models") or [])
                if isinstance(m, str) and m.strip()
            )
            if not models:
                return "ERROR — pass `models`: the semantic model name(s) you connected to."
            import threading as _threading

            _box: dict = {}

            def _refresh_runner():
                try:
                    _box["out"] = asyncio.run(
                        prefetch_metadata(get_token_cached(), models)
                    )
                except Exception as _e:
                    _box["err"] = f"{type(_e).__name__}: {_e}"

            _t = _threading.Thread(target=_refresh_runner, daemon=True)
            _t.start()
            _t.join(timeout=180)
            if _t.is_alive():
                return "ERROR — registry refresh timed out after 180s."
            if _box.get("err"):
                return f"ERROR — registry refresh failed: {_box['err']}"
            _note = ""
            if sorted(models) != sorted(_active_models()):
                _note = (
                    f" NOTE: this session validates binds against "
                    f"{sorted(_active_models())} — if binds still fail, refresh "
                    f"those models instead."
                )
            return (
                f"Registry refreshed for {list(models)} "
                f"({len(_box.get('out') or '')} chars of metadata) — retry your binds now."
                + _note
            )

        elif tool_name == "pbi_visual_bind":
            _err = _validate_bind_fields(tool_input)
            if _err:
                return _err

            # Roles bound in-process via visual_bind backend
            _BACKEND_ONLY_ROLES = (
                "x",
                "y",
                "detail",
                "size",
                "line",
                "column",
                "trend",
                "breakdown",
            )
            _backend_roles_used = [r for r in _BACKEND_ONLY_ROLES if tool_input.get(r)]
            _backend_bound_note = ""
            if _backend_roles_used:
                try:
                    from pbi_cli.core.pbir_path import resolve_report_path
                    from pbi_cli.core.visual_backend import (
                        visual_bind as _pbi_backend_bind,
                    )

                    _backend_result = _pbi_backend_bind(
                        definition_path=resolve_report_path(
                            tool_input.get("report_path", "")
                        ),
                        page_name=tool_input["page"],
                        visual_name=tool_input["visual_name"],
                        bindings=[
                            {"role": r, "field": tool_input[r]}
                            for r in _backend_roles_used
                        ],
                    )
                except Exception as _be:
                    return (
                        f"ERROR — bind failed for role(s) {_backend_roles_used}: {_be}"
                    )
                _vglob = glob.glob(
                    f"{tool_input.get('report_path', '')}/**/visuals/"
                    f"{tool_input['visual_name']}/visual.json",
                    recursive=True,
                )
                if _vglob:
                    _trace_file_after_write(_vglob[0], "pbi_visual_bind:backend-role")
                _backend_bound_note = f"bound: {_backend_result['bindings']}"

            def _normalize_bound_visual() -> None:
                """Repair roles pbi_cli wrote wrong, on EVERY bind exit path.

                A bind carrying only backend roles (line/x/y/detail/size)
                returns before the CLI ever runs, and `line=` alone on a combo
                chart is precisely the "add the target series" call the
                blank-visual incident was about — so normalizing only on the
                CLI path would leave the original bug reachable.
                """
                try:
                    for _vp in glob.glob(
                        f"{report_path}/**/visuals/"
                        f"{tool_input['visual_name']}/visual.json",
                        recursive=True,
                    ):
                        with open(_vp, "r", encoding="utf-8") as _f:
                            _vdata = json.load(_f)
                        if normalize_pbir_query_state(_vdata):
                            _atomic_write_json(_vp, _vdata, indent=2)
                            _trace_file_after_write(
                                _vp, "pbi_visual_bind:role-normalize"
                            )
                except Exception as _norm_err:
                    # Not fatal — the bind itself succeeded — but never silent:
                    # swallowing this is exactly how the predecessor of this
                    # repair disabled itself unnoticed.
                    logger.warning(
                        "pbi_visual_bind: role normalization failed for %s: %s",
                        tool_input.get("visual_name"),
                        _norm_err,
                    )

            # Only roles the CLI actually has a flag for belong here. Listing
            # column/trend/breakdown made a matrix bound with row+column, or a
            # kpi bound with indicator+trend, take the CLI path and build a
            # command with no role flags at all.
            _cli_roles_present = any(
                tool_input.get(role)
                for role in (
                    "field",
                    "category",
                    "value",
                    "row",
                    "legend",
                    "indicator",
                    "goal",
                )
            )
            if not _cli_roles_present:
                _normalize_bound_visual()
                return _backend_bound_note or (
                    "ERROR — bind blocked: no recognized binding role provided."
                )

            cmd = (
                [*PBI_CLI, "visual", "--no-sync"]
                + path_args
                + [
                    "bind",
                    tool_input["visual_name"],
                    "--page",
                    tool_input["page"],
                ]
            )
            measure_reg = _load_measure_registry()
            column_reg = _load_column_registry()
            bare_measure_names = {
                k.split(".", 1)[1] for k in measure_reg if k.startswith("*.")
            }

            _card_like = {"card", "cardVisual", "kpi", "kpiNew", "gauge"}
            visual_is_card = False
            if report_path:
                _vpath = glob.glob(
                    f"{report_path}/**/visuals/{tool_input['visual_name']}/visual.json",
                    recursive=True,
                )
                if _vpath:
                    try:
                        with open(_vpath[0], encoding="utf-8") as _vf:
                            _vt = json.load(_vf).get("visual", {}).get("visualType", "")
                        visual_is_card = _vt in _card_like
                    except Exception:
                        pass

            column_fields = set()
            measure_fields = set()

            for role in [
                "field",
                "category",
                "value",
                "row",
                "column",
                "legend",
                "indicator",
                "trend",
                "goal",
                "breakdown",
            ]:
                if tool_input.get(role):
                    # Backend-bound roles have no corresponding CLI flag —
                    # forwarding one makes Click exit 2 and takes the whole
                    # bind down with it. They still have to run through the
                    # rest of this loop, which is what classifies each field
                    # as Measure vs Column for the post-write retag below.
                    if role not in _BACKEND_ONLY_ROLES:
                        cmd += [f"--{role}", tool_input[role]]
                    raw = tool_input[role]
                    if "[" in raw:
                        table = raw.split("[")[0].strip().strip("'").strip('"')
                        field_name = raw.split("[")[1].rstrip("]")
                        registry_key = f"{table}.{field_name}"
                    else:
                        field_name = raw
                        registry_key = None

                    is_measure = (
                        (registry_key and registry_key in measure_reg)
                        or field_name in bare_measure_names
                        or any(k.endswith(f".{field_name}") for k in measure_reg)
                    )
                    is_column = bool(registry_key and registry_key in column_reg)

                    if is_measure:
                        measure_fields.add(field_name)
                    elif is_column or role in (
                        "category",
                        "row",
                        "detail",
                        "legend",
                        "trend",
                        "breakdown",
                    ):
                        column_fields.add(field_name)
                    elif (
                        role
                        in (
                            "value",
                            "indicator",
                            "goal",
                            "max",
                            "line",
                            "column",
                        )
                        or role == "field"
                        and visual_is_card
                    ):
                        measure_fields.add(field_name)
                    else:
                        column_fields.add(field_name)

            result = subprocess.run(
                cmd,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
            output = ((result.stdout or "") + (result.stderr or "")).strip()
            _err = _pbi_exit_error(tool_name, result, output)
            if _err:
                return _err

            if report_path:
                vpath_matches = glob.glob(
                    f"{report_path}/**/visuals/{tool_input['visual_name']}/visual.json",
                    recursive=True,
                )
                for vpath in vpath_matches:
                    try:
                        with open(vpath, encoding="utf-8") as f:
                            vdata = json.load(f)
                        visual = vdata.get("visual", {})
                        visual_type = visual.get("visualType", "")
                        slicer_types = {
                            "slicer",
                            "listSlicer",
                            "textSlicer",
                            "advancedSlicerVisual",
                        }
                        is_slicer = visual_type in slicer_types
                        if is_slicer and tool_input.get("field"):
                            fn = tool_input["field"]
                            if "[" in fn:
                                fn = fn.split("[")[1].rstrip("]")
                            column_fields.add(fn)

                        changed = False

                        def _fix_refs(node):
                            nonlocal changed
                            if isinstance(node, dict):
                                node.pop("active", None)

                                src = node.get("SourceRef")
                                if isinstance(src, dict):
                                    ent = src.get("Entity")
                                    if isinstance(ent, str) and "'" in ent:
                                        src["Entity"] = ent.strip().strip("'")
                                        changed = True

                                for key in ("queryRef", "nativeQueryRef"):
                                    val = node.get(key)
                                    if isinstance(val, str) and "'" in val:
                                        node[key] = val.replace("'", "")
                                        changed = True

                                prop_name = None
                                for ref_type in ("Measure", "Column"):
                                    if ref_type in node and isinstance(
                                        node[ref_type], dict
                                    ):
                                        prop_name = node[ref_type].get("Property", "")
                                        if (
                                            prop_name in column_fields
                                            and ref_type == "Measure"
                                        ):
                                            node["Column"] = node.pop("Measure")
                                            changed = True
                                        elif (
                                            prop_name in measure_fields
                                            and ref_type == "Column"
                                        ):
                                            node["Measure"] = node.pop("Column")
                                            changed = True
                                        break
                                for v in list(node.values()):
                                    _fix_refs(v)
                            elif isinstance(node, list):
                                for item in node:
                                    _fix_refs(item)

                        _fix_refs(vdata)

                        if is_slicer:
                            vdata["visual"]["visualType"] = "slicer"
                            _s_objects = vdata["visual"].setdefault("objects", {})
                            _s_objects["data"] = [
                                {
                                    "properties": {
                                        "mode": {
                                            "expr": {"Literal": {"Value": "'Dropdown'"}}
                                        }
                                    }
                                }
                            ]
                            _s_objects["header"] = [
                                {
                                    "properties": {
                                        "show": {
                                            "expr": {"Literal": {"Value": "false"}}
                                        }
                                    }
                                }
                            ]
                            changed = True

                        try:
                            import secrets as _secrets

                            existing_filters = (
                                vdata.get("filterConfig", {}).get("filters", [])
                                if isinstance(vdata.get("filterConfig"), dict)
                                else []
                            )
                            existing_keys = {
                                (
                                    next(iter(ff.get("field", {}))),
                                    ff.get("field", {})
                                    .get(next(iter(ff.get("field", {}))), {})
                                    .get("Expression", {})
                                    .get("SourceRef", {})
                                    .get("Entity", ""),
                                    ff.get("field", {})
                                    .get(next(iter(ff.get("field", {}))), {})
                                    .get("Property", ""),
                                )
                                for ff in existing_filters
                                if ff.get("field")
                            }
                            new_filters = list(existing_filters)
                            qs2 = visual.get("query", {}).get("queryState", {})
                            for _body in qs2.values():
                                for _p in _body.get("projections", []) or []:
                                    _f = _p.get("field")
                                    if not isinstance(_f, dict):
                                        continue
                                    _kind = next(iter(_f), None)
                                    if _kind not in ("Measure", "Column"):
                                        continue
                                    _ent = (
                                        _f[_kind]
                                        .get("Expression", {})
                                        .get("SourceRef", {})
                                        .get("Entity", "")
                                    )
                                    _prop = _f[_kind].get("Property", "")
                                    if (_kind, _ent, _prop) in existing_keys:
                                        continue
                                    existing_keys.add((_kind, _ent, _prop))
                                    new_filters.append(
                                        {
                                            "name": _secrets.token_hex(10),
                                            "field": {_kind: _f[_kind]},
                                            "type": (
                                                "Advanced"
                                                if _kind == "Measure"
                                                else "Categorical"
                                            ),
                                        }
                                    )
                            if new_filters and (
                                len(new_filters) != len(existing_filters)
                            ):
                                vdata["filterConfig"] = {"filters": new_filters}
                                changed = True
                        except Exception:
                            pass

                        if changed:
                            _atomic_write_json(vpath, vdata, indent=2)
                            _trace_file_after_write(vpath, "pbi_visual_bind:post-fix")
                            fixes = []
                            if column_fields:
                                fixes.append(f"columns={list(column_fields)}")
                            if measure_fields:
                                fixes.append(f"measures={list(measure_fields)}")
                            if is_slicer:
                                fixes.append("dropdown")
                            output += f" [fixed: {', '.join(fixes)}]"
                    except Exception:
                        pass
            _normalize_bound_visual()

            if _backend_bound_note:
                output = f"{output}\n{_backend_bound_note}" if output else _backend_bound_note
            return output or "Done."

        elif tool_name == "pbi_visual_format":
            from report.visual_format import apply_format

            return apply_format(
                report_path,
                tool_input["visual_name"],
                data_label_show=tool_input.get("data_label_show"),
                data_label_color=tool_input.get("data_label_color"),
                data_label_size=tool_input.get("data_label_size"),
                axis_show=tool_input.get("axis_show"),
                axis_color=tool_input.get("axis_color"),
                legend_show=tool_input.get("legend_show"),
                legend_position=tool_input.get("legend_position"),
                background_color=tool_input.get("background_color"),
                title_color=tool_input.get("title_color"),
                title_size=tool_input.get("title_size"),
                title_align=tool_input.get("title_align"),
                data_colors=tool_input.get("data_colors"),
                page=tool_input.get("page"),
            )

        elif tool_name == "pbi_visual_conditional_color":
            from report.visual_format import apply_conditional_color

            _err, _note = _validate_format_field(
                tool_input["target_table"], tool_input["target_measure"], tool_input
            )
            if _err:
                return _err
            _out = apply_conditional_color(
                report_path,
                tool_input["visual_name"],
                target_table=tool_input["target_table"],
                target_measure=tool_input["target_measure"],
                min_color=tool_input["min_color"],
                max_color=tool_input["max_color"],
                mid_color=tool_input.get("mid_color"),
                page=tool_input.get("page"),
            )
            if _note and "ERROR" not in _out:
                _out += _note
            return _out

        elif tool_name == "pbi_visual_color_by_measure":
            from report.visual_format import color_bars_by_measure

            _err, _note = _validate_format_field(
                tool_input["target_table"], tool_input["target_measure"], tool_input
            )
            if _err:
                return _err
            _out = color_bars_by_measure(
                report_path,
                tool_input["visual_name"],
                target_table=tool_input["target_table"],
                target_measure=tool_input["target_measure"],
                min_color=tool_input["min_color"],
                max_color=tool_input["max_color"],
                mid_color=tool_input.get("mid_color"),
                page=tool_input.get("page"),
            )
            if _note and "ERROR" not in _out:
                _out += _note
            return _out

        elif tool_name == "pbi_set_report_style":
            page_bg = tool_input.get("page_background")
            msg = inject_theme(
                report_path,
                palette=tool_input.get("data_colors"),
                accent=tool_input.get("accent"),
                page_bg=page_bg,
                good=tool_input.get("good"),
                bad=tool_input.get("bad"),
            )
            if page_bg:
                from report.dashboard_polish import _backup_file

                _bg_ok = 0
                _bg_failed: list[str] = []
                for pj in glob.glob(
                    f"{report_path}/**/pages/*/page.json", recursive=True
                ):
                    try:
                        with open(pj, encoding="utf-8") as f:
                            page = json.load(f)
                        page.setdefault("objects", {})
                        page["objects"]["background"] = [
                            {
                                "properties": {
                                    "color": {
                                        "solid": {
                                            "color": {
                                                "expr": {
                                                    "Literal": {"Value": f"'{page_bg}'"}
                                                }
                                            }
                                        }
                                    },
                                    "transparency": {
                                        "expr": {"Literal": {"Value": "0D"}}
                                    },
                                }
                            }
                        ]
                        _backup_file(pj)
                        _atomic_write_json(pj, page, indent=2)
                        _bg_ok += 1
                    except Exception as _e:
                        _bg_failed.append(
                            f"{os.path.basename(os.path.dirname(pj))}: {_e}"
                        )
                msg += f" [page backgrounds → {page_bg}: {_bg_ok} updated"
                if _bg_failed:
                    msg += f", {len(_bg_failed)} FAILED ({'; '.join(_bg_failed)})"
                msg += "]"
            try:
                from report.visual_format import style_kpi_card

                _theme = _registered_theme(report_path)
                _accent = (
                    _theme.get("tableAccent") or (_theme.get("dataColors") or [""])[0]
                )
                _restyled = 0
                _card_failed = 0
                if _accent:
                    for _vp in glob.glob(
                        f"{report_path}/**/visuals/*/visual.json", recursive=True
                    ):
                        try:
                            with open(_vp, encoding="utf-8") as _vf:
                                _vd = json.load(_vf)
                            if _vd.get("visual", {}).get("visualType") == "cardVisual":
                                style_kpi_card(
                                    report_path,
                                    _vd.get("name", ""),
                                    accent_color=_accent,
                                )
                                _restyled += 1
                        except Exception:
                            _card_failed += 1
                            continue
                if _restyled:
                    msg += f" [restyled {_restyled} KPI card(s) → {_accent}]"
                if _card_failed:
                    msg += f" [{_card_failed} KPI card(s) FAILED to restyle]"
            except Exception as _e:
                msg += f" [card restyle skipped: {_e}]"
            return msg

        elif tool_name == "pbi_visual_color_by_category":
            from report.visual_format import color_by_category

            return color_by_category(
                report_path,
                tool_input["visual_name"],
                table=tool_input["table"],
                column=tool_input["column"],
                category_colors=tool_input["category_colors"],
                is_numeric=bool(tool_input.get("is_numeric")),
                page=tool_input.get("page"),
            )

        elif tool_name == "pbi_visual_add_button":
            _xywh_err = _require_xywh(tool_name, tool_input)
            if _xywh_err:
                return _xywh_err
            from report.visual_format import create_action_button

            return create_action_button(
                report_path,
                tool_input["page"],
                tool_input["name"],
                tool_input["x"],
                tool_input["y"],
                tool_input["width"],
                tool_input["height"],
                label=tool_input.get("label", ""),
                shape=tool_input.get("shape", "Blank"),
                action_type=tool_input.get("action_type"),
                action_target=tool_input.get("action_target"),
                fill_color=tool_input.get("fill_color"),
                text_color=tool_input.get("text_color"),
                theme=_registered_theme(report_path),
            )

        elif tool_name == "pbi_visual_add_page_nav":
            _xywh_err = _require_xywh(tool_name, tool_input)
            if _xywh_err:
                return _xywh_err
            from report.visual_format import create_page_navigator

            return create_page_navigator(
                report_path,
                tool_input["page"],
                tool_input["name"],
                tool_input["x"],
                tool_input["y"],
                tool_input["width"],
                tool_input["height"],
                orientation=tool_input.get("orientation", "horizontal"),
                selected_color=tool_input.get("selected_color"),
                unselected_color=tool_input.get("unselected_color"),
                theme=_registered_theme(report_path),
            )

        elif tool_name == "pbi_visual_set_container":
            cmd = (
                [*PBI_CLI, "visual", "--no-sync"]
                + path_args
                + [
                    "set-container",
                    tool_input["visual_name"],
                    "--page",
                    tool_input["page"],
                ]
            )
            if tool_input.get("title"):
                cmd += ["--title", tool_input["title"]]
            if tool_input.get("border_show") is not None:
                cmd += ["--border-show", str(tool_input["border_show"]).lower()]
            if tool_input.get("background_show") is not None:
                cmd += ["--background-show", str(tool_input["background_show"]).lower()]

        elif tool_name == "pbi_page_clear_visuals":
            page_dirs = glob.glob(
                f"{report_path}/**/pages/{tool_input['page']}/visuals", recursive=True
            )
            if not page_dirs:
                page_exists = glob.glob(
                    f"{report_path}/**/pages/{tool_input['page']}", recursive=True
                )
                if page_exists:
                    return f"Page '{tool_input['page']}' has no visuals to clear."
                return (
                    f"ERROR — page '{tool_input['page']}' not found. "
                    f"Call pbi_report_info to list the actual page names; "
                    f"nothing was deleted."
                )
            visuals_dir = page_dirs[0]

            if visuals_dir and os.path.isdir(visuals_dir):
                count = 0
                locked: list[str] = []
                for vdir in os.listdir(visuals_dir):
                    full = os.path.join(visuals_dir, vdir)
                    if os.path.isdir(full):
                        if _rmtree_resilient(full):
                            count += 1
                        else:
                            locked.append(vdir)
                if locked:
                    return (
                        f"Deleted {count} visual(s) from '{tool_input['page']}', but "
                        f"{len(locked)} folder(s) are LOCKED by OneDrive sync or an open "
                        f"Power BI Desktop: {locked}. This is a TRANSIENT file lock — do "
                        f"NOT retry the delete in a loop (that wastes turns). Close the "
                        f".pbip in Desktop or pause OneDrive, or just continue; the lock "
                        f"clears on the next sync."
                    )
                return f"Deleted {count} visuals from page '{tool_input['page']}'. Page is now clean."
            return f"Could not find visuals directory for page '{tool_input['page']}'."

        elif tool_name == "pbi_visual_update":
            cmd = (
                [*PBI_CLI, "visual", "--no-sync"]
                + path_args
                + [
                    "update",
                    tool_input["visual_name"],
                    "--page",
                    tool_input["page"],
                ]
            )
            for attr in ["x", "y", "width", "height"]:
                if tool_input.get(attr) is not None:
                    cmd += [f"--{attr}", str(_snap_px(tool_input[attr]))]

        elif tool_name == "pbi_visual_delete":
            cmd = (
                [*PBI_CLI, "visual", "--no-sync"]
                + path_args
                + [
                    "delete",
                    tool_input["visual_name"],
                    "--page",
                    tool_input["page"],
                ]
            )

        elif tool_name == "pbi_report_info":
            info_cmd = [*PBI_CLI, "--json", "report", "--no-sync"] + path_args + ["info"]
            info_result = subprocess.run(
                info_cmd,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
            info_out = ((info_result.stdout or "") + (info_result.stderr or "")).strip()
            pages_cmd = [*PBI_CLI, "--json", "report", "--no-sync"] + path_args + ["list-pages"]
            try:
                pages_result = subprocess.run(
                    pages_cmd,
                    capture_output=True,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                    check=False,
                )
                if pages_result.returncode == 0:
                    return (
                        _merge_page_dims(info_out, (pages_result.stdout or "").strip())
                        or "Done."
                    )
            except Exception:
                pass
            return info_out + "\n[pbi_report_info] note: dimensions unavailable"

        elif tool_name == "pbi_report_reload":
            cmd = [*PBI_CLI, "report"] + path_args + ["reload"]
            result = subprocess.run(
                cmd,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
            output = ((result.stdout or "") + (result.stderr or "")).strip()
            if "ActivePageName" in output or "no pages" in output.lower():
                return "Reload skipped — open the .pbip file manually in Power BI Desktop to see changes."
            return output or "Reloaded."

        elif tool_name == "pbi_visual_list":
            cmd = (
                [*PBI_CLI, "--json", "visual", "--no-sync"]
                + path_args
                + ["list", "--page", tool_input["page"]]
            )

        elif tool_name == "pbi_visual_bindings":
            return _pbi_visual_bindings(report_path, tool_input.get("page") or None)

        elif tool_name == "pbi_wire_tooltip_page":
            return _wire_tooltip_page(
                report_path,
                tool_input["tooltip_page"],
                tool_input["source_page"],
                tool_input["source_visual"],
                width=tool_input.get("width", 360),
                height=tool_input.get("height", 260),
            )

        elif tool_name == "pbi_wire_drillthrough_page":
            from report.dashboard_polish import wire_drillthrough_page

            return wire_drillthrough_page(
                report_path, tool_input["target_page"], tool_input["field"]
            )

        elif tool_name == "pbi_page_set_background":
            cmd = (
                [*PBI_CLI, "report", "--no-sync"]
                + path_args
                + [
                    "set-background",
                    tool_input["page"],
                    "--color",
                    tool_input["color"],
                ]
            )
            if tool_input.get("transparency") is not None:
                cmd += ["--transparency", str(int(tool_input["transparency"]))]

        elif tool_name == "pbi_page_set_background_image":
            from report.dashboard_polish import set_page_background_image

            return set_page_background_image(
                report_path,
                tool_input["page"],
                tool_input["image_path"],
                scaling=tool_input.get("scaling", "Fit"),
                layer=tool_input.get("layer", "canvas"),
            )

        elif tool_name == "pbi_visual_add_image":
            _xywh_err = _require_xywh(tool_name, tool_input)
            if _xywh_err:
                return _xywh_err
            from report.dashboard_polish import add_image_visual

            return add_image_visual(
                report_path,
                tool_input["page"],
                tool_input["image_path"],
                tool_input["visual_name"],
                int(tool_input["x"]),
                int(tool_input["y"]),
                int(tool_input["width"]),
                int(tool_input["height"]),
                scaling=tool_input.get("scaling", "Fit"),
            )

        elif tool_name == "pbi_report_set_theme":
            cmd = (
                [*PBI_CLI, "report", "--no-sync"]
                + path_args
                + [
                    "set-theme",
                    "--file",
                    tool_input["theme_file"],
                ]
            )

        elif tool_name == "pbi_report_validate":
            cmd = [*PBI_CLI, "report", "--no-sync"] + path_args + ["validate"]

        elif tool_name == "pbi_dashboard_audit":
            audit = _audit_dashboard(report_path)
            return json.dumps(audit, indent=2)

        elif tool_name == "pbi_report_health_check":
            health = _health_check_report(report_path)
            return json.dumps(health, indent=2)

        elif tool_name == "pbi_desktop_status":
            from fabric.desktop_bridge import BridgeNotAvailable, get_status

            try:
                instances = get_status()
            except BridgeNotAvailable as e:
                return f"Desktop Bridge unavailable: {e}"
            if not instances:
                return (
                    "Desktop Bridge is reachable but no Power BI Desktop "
                    "instance is currently running."
                )
            return json.dumps(instances, indent=2)

        elif tool_name == "pbi_desktop_sync":
            from fabric.desktop_bridge import (
                BridgeNotAvailable,
                ensure_pid,
                screenshot_all,
            )
            from fabric.desktop_bridge import reload as _bridge_reload

            pbip = tool_input["pbip_path"].strip().rstrip("\\/")
            if pbip.endswith(".Report"):
                pbip = pbip[: -len(".Report")] + ".pbip"
            if not os.path.isfile(pbip):
                return f"ERROR — .pbip file not found: {pbip}"
            try:
                pid = ensure_pid(pbip)
                _bridge_reload(pid)
                shots = screenshot_all(
                    pid, os.path.join(os.path.dirname(pbip), "screenshots")
                )
            except BridgeNotAvailable as e:
                return f"Desktop Bridge unavailable: {e}"
            except Exception as e:
                return f"ERROR — pbi_desktop_sync failed: {type(e).__name__}: {e}"
            if not shots:
                return f"Desktop (pid {pid}) reloaded — no screenshots returned."
            return f"Desktop (pid {pid}) reloaded. Page screenshots:\n" + "\n".join(
                str(p) for p in shots
            )

        else:
            return f"Unknown pbi tool: {tool_name}"

        # encoding pinned: text=True alone decodes as cp1252 on Windows and
        result = subprocess.run(
            cmd,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        if result.returncode != 0:
            return (
                f"ERROR — tool '{tool_name}' failed (exit {result.returncode}): "
                f"{output or '(no output)'}. Nothing may have been written — "
                f"verify with pbi_visual_list / pbi_report_info before continuing."
            )

        if tool_name == "pbi_visual_update":
            # Moving a visual can land it on another just as easily as creating
            # one there. This is the fall-through path, so the note is applied
            # after the CLI has written the new position.
            output = (output or "Done.") + _overlap_note_for(tool_input)

        return output or "Done."

    except subprocess.TimeoutExpired as e:
        partial = ((e.stdout or "") + (e.stderr or "")).strip()
        _build_trace("tool_cli_timeout", tool=tool_name, cmd=e.cmd, partial=partial[:2000])
        return (
            f"Error: {tool_name} timed out after 30s. "
            "Inspect the target before retrying a write; its outcome may be partial."
            + (f"\nCLI output before the timeout:\n{partial[:1500]}" if partial else "")
        )
    except Exception as e:
        return f"Error running pbi command: {e}"


def _merge_page_dims(info_json: str, list_pages_json: str) -> str:
    try:
        info = json.loads(info_json)
        pages_dims = json.loads(list_pages_json)
        if not isinstance(pages_dims, list):
            raise TypeError("list-pages did not return an array")
        by_name = {p.get("name"): p for p in pages_dims if isinstance(p, dict)}
        for page in info.get("pages", []):
            dims = by_name.get(page.get("name"))
            if dims:
                for key in ("width", "height", "display_option"):
                    if key in dims:
                        page[key] = dims[key]
        return json.dumps(info, indent=2)
    except Exception:
        return info_json + "\n[pbi_report_info] note: dimensions unavailable"


def _lit_dyn(val: str) -> dict:
    return {"expr": {"Literal": {"Value": val}}}


def _apply_page_background_dynamic(page_folder: str, display_name: str) -> str:
    if not os.path.isdir(page_folder):
        return f"[polish] skip — page folder not found: {page_folder}"

    page_json_path = os.path.join(page_folder, "page.json")
    if os.path.isfile(page_json_path):
        with open(page_json_path, encoding="utf-8") as f:
            page = json.load(f)
        page.setdefault("objects", {})
        page["objects"]["background"] = [
            {
                "properties": {
                    "color": {"solid": {"color": _lit_dyn(f"'{REPORT_PAGE_BG}'")}},
                    "transparency": _lit_dyn("0D"),
                }
            }
        ]
        _atomic_write_json(page_json_path, page, indent=2)
    return f"[polish] page background set on {os.path.basename(page_folder)}"
