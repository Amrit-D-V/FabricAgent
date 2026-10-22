"""Semantic-model metadata prefetch over the powerbi_modeling MCP server.

Extracted from app.py: spawns the MCP server (mcp_launch_command), pulls
table/column/measure metadata per model, and refreshes the measure/column
registries used to ground the assistant. UI-free; app.py caches the result
per session via _fetch_and_cache_metadata.
"""

import asyncio
import json
import os

from auth.token_manager import get_token_cached
from fabric import workspace_context as _ws_ctx
from fabric.workspace_context import (
    _active_workspace,
    _load_column_registry,
    _load_measure_registry,
    _read_registry_json,
    _registry_path,
)
from model.json_io import _atomic_write_json
from core import runtime_state as _state
from util.logger_setup import get_logger

logger = get_logger(__name__)


def _parse_mcp_json(result) -> dict | None:
    content = getattr(result, "content", None) or []
    for block in content:
        txt = getattr(block, "text", None)
        if not txt:
            continue
        try:
            return json.loads(txt)
        except (json.JSONDecodeError, TypeError):
            continue
    return None


_DATATYPE_TAGS = {
    2: "text",
    6: "int",
    7: "int",
    8: "double",
    9: "date",
    10: "decimal",
    11: "bool",
}
_DATATYPE_STR = {
    "string": "text",
    "int64": "int",
    "double": "double",
    "datetime": "date",
    "decimal": "decimal",
    "boolean": "bool",
    "currency": "decimal",
}


def _datatype_tag(raw) -> str:
    if raw is None or isinstance(raw, bool):
        return ""
    if isinstance(raw, (int, float)):
        return _DATATYPE_TAGS.get(int(raw), "")
    s = str(raw).strip().lower()
    return _DATATYPE_STR.get(s, s if s.isalpha() else "")


def _validate_dax_table_refs(query: str) -> str | None:
    import re as _re

    if not query or "INFO." in query.upper():
        return None

    refs = set(_re.findall(r"'([^']+)'", query))
    if not refs:
        return None

    known: set[str] = set()
    col_reg = _load_column_registry()
    mea_reg = _load_measure_registry()
    table_cols: dict[str, list[str]] = {}
    for key in col_reg:
        if key == "_meta":
            continue
        tbl, _, col = key.partition(".")
        if tbl:
            known.add(tbl)
            table_cols.setdefault(tbl, []).append(col)
    for key in mea_reg:
        if key == "_meta":
            continue
        tbl, _, _ = key.partition(".")
        if tbl:
            known.add(tbl)

    if not known:
        return None  # no registry loaded yet — can't validate

    bad = refs - known
    if not bad:
        return None

    hint_lines = [f"Valid tables: {', '.join(sorted(known))}"]
    for tbl in sorted(known):
        cols = table_cols.get(tbl)
        if cols:
            hint_lines.append(f"  {tbl}: {', '.join(cols[:15])}")
    hint = "\n".join(hint_lines)

    return (
        f"ERROR — DAX query references non-existent table(s): "
        f"{', '.join(sorted(bad))}. "
        f"Use ONLY exact table names from the model metadata. "
        f"There is NO separate Product/Date/Category table — those are "
        f"COLUMNS inside existing tables.\n{hint}"
    )


def _fetch_single_model(
    workspace: str, model_name: str, measure_reg: dict, column_reg: dict
) -> str:
    from fabric import pbi_xmla

    conn_name = f"Fabric-{workspace}-{model_name}"
    server = f"powerbi://api.powerbi.com/v1.0/myorg/{workspace}"
    meta = pbi_xmla.fetch_model_metadata(server, model_name)
    print(f"   Fetched {model_name}")

    lines = [
        f"\n{'=' * 40}",
        f"MODEL: {workspace}/{model_name}",
        f"CONNECTION: {conn_name}",
    ]

    table_names = meta["tables"]
    if table_names:
        lines.append(f"TABLES ({len(table_names)}): {', '.join(table_names)}")
        _rc = meta.get("row_counts") or {}
        _counts = [f"{t}={_rc[t]}" for t in _rc]
        if _counts:
            lines.append("ROW COUNTS: " + ", ".join(_counts))

    measure_by_table = meta["measures_by_table"]
    if measure_by_table:
        lines.append(
            "MEASURES BY TABLE (use exact Table[Measure] format when binding):"
        )
        for tname, measures in sorted(measure_by_table.items()):
            lines.append(f"  {tname}: {', '.join(measures)}")
        for tname, measures in measure_by_table.items():
            for m in measures:
                measure_reg[f"{tname}.{m}"] = True

    if meta["measure_errors"]:
        lines.append(f"MEASURES WITH ERRORS: {'; '.join(meta['measure_errors'])}")

    columns_by_table = meta["columns_by_table"]
    if columns_by_table:
        table_cols: dict[str, list[str]] = {}
        for tname, cols in columns_by_table.items():
            for col, dtype in cols:
                _tag = _datatype_tag(dtype)
                table_cols.setdefault(tname, []).append(
                    f"{col} ({_tag})" if _tag else col
                )
                column_reg[f"{tname}.{col}"] = True
        lines.append(
            "COLUMNS BY TABLE (data type in parens — only int/double/decimal "
            "belong on a value axis or in SUM/AVG; text/date are categories/axes):"
        )
        for tname, cols in sorted(table_cols.items()):
            lines.append(f"  {tname}: {', '.join(cols)}")

    rels = meta["relationships"]
    if rels:
        lines.append(f"RELATIONSHIPS ({len(rels)} — [join column] on each side):")
        for r in rels:
            state = "active" if r.get("is_active") else "inactive"
            lines.append(f"  {pbi_xmla.relationship_label(r)} ({state})")

    return "\n".join(lines)


def _mcp_env(access_token: str) -> dict:
    env = dict(os.environ)
    from fabric.fabric_connection import get_fabric_connection

    _c = get_fabric_connection()
    if _c.has_sp:
        env["AZURE_TENANT_ID"] = _c.tenant_id
        env["AZURE_CLIENT_ID"] = _c.client_id
        env["AZURE_CLIENT_SECRET"] = _c.client_secret
    _has_sp = all(
        env.get(k)
        for k in ("AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_CLIENT_SECRET")
    )
    if _has_sp:
        env["AZURE_TOKEN_CREDENTIALS"] = "prod"
        env.pop("PBI_MODELING_MCP_ACCESS_TOKEN", None)
    else:
        try:
            token = get_token_cached()
        except Exception:
            token = access_token or ""
        if token:
            env["PBI_MODELING_MCP_ACCESS_TOKEN"] = token
    logger.debug(
        "MCP_AUTH_DIAG | AZURE_TOKEN_CREDENTIALS=%r | sp_creds_present=%s | "
        "bearer_token_set=%s | bearer_len=%s",
        env.get("AZURE_TOKEN_CREDENTIALS"),
        _has_sp,
        bool(env.get("PBI_MODELING_MCP_ACCESS_TOKEN")),
        len(env.get("PBI_MODELING_MCP_ACCESS_TOKEN", "")),
    )
    if not _has_sp and not env.get("PBI_MODELING_MCP_ACCESS_TOKEN"):
        logger.warning(
            "MCP auth: no service-principal creds and no bearer token — "
            "the MCP server may fall back to an interactive browser prompt."
        )
    return env


async def prefetch_metadata(access_token: str, models: tuple[str, ...] = ()) -> str:
    target_models = list(models) if models else _ws_ctx.FABRIC_MODELS
    _ws = _active_workspace()

    print(
        f" Pre-fetching metadata for {len(target_models)} model(s): {', '.join(target_models)}"
    )
    measure_reg: dict = {}
    column_reg: dict = {}
    _prev_measures = {
        k: v
        for k, v in _read_registry_json(
            _registry_path("measure", tuple(target_models))
        ).items()
        if not k.startswith("_")
    }
    _prev_columns = {
        k: v
        for k, v in _read_registry_json(
            _registry_path("column", tuple(target_models))
        ).items()
        if not k.startswith("_")
    }

    _last_good: dict = getattr(_state, "last_good_model_meta", None) or {}
    _had_failure = False
    all_metadata = []

    async def _fetch_one(model_name: str):
        try:
            meta = await asyncio.to_thread(
                _fetch_single_model, _ws, model_name, measure_reg, column_reg
            )
            return model_name, meta, True
        except Exception as e:
            return model_name, e, False

    _results = await asyncio.gather(*(_fetch_one(m) for m in target_models))
    for model_name, payload, _ok in _results:
        if _ok:
            all_metadata.append(payload)
            _last_good[model_name] = payload
        else:
            print(f"   Failed to fetch {model_name}: {payload}")
            _had_failure = True
            if model_name in _last_good:
                print("   Using last-good cached metadata for this model.")
                all_metadata.append(_last_good[model_name])
            else:
                all_metadata.append(f"\nMODEL: {_ws}/{model_name}\nERROR: {payload}")
    _state.last_good_model_meta = _last_good
    if _had_failure:
        for _k, _v in _prev_measures.items():
            measure_reg.setdefault(_k, _v)
        for _k, _v in _prev_columns.items():
            column_reg.setdefault(_k, _v)

    metadata = "\n".join(all_metadata)
    metadata += "\n\nINFO_COLUMNS: MEASURES→[ID,TableID,Name,Expression,Description,State,ErrorMessage,IsHidden] TABLES→[ID,Name,IsHidden,IsPrivate] COLUMNS→[ID,TableID,ExplicitName,InferredName,ExplicitDataType,Type,IsHidden,State] RELATIONSHIPS→[ID,FromTableID,FromColumnID,ToTableID,ToColumnID,IsActive,CrossFilteringBehavior]"

    here = os.path.dirname(__file__)
    try:
        import hashlib as _hashlib
        from datetime import datetime as _dt

        _model_list = sorted(target_models)
        _meta = {
            "model_names": _model_list,
            "workspace": _ws,
            "generated_at": _dt.now().isoformat(timespec="seconds"),
            "fingerprint": _hashlib.sha1(
                json.dumps([_ws, _model_list]).encode()
            ).hexdigest()[:12],
        }

        m_reg = dict(measure_reg)
        m_reg["_meta"] = _meta
        c_reg = dict(column_reg)
        c_reg["_meta"] = _meta

        _atomic_write_json(_registry_path("measure", tuple(target_models)), m_reg)
        _atomic_write_json(_registry_path("column", tuple(target_models)), c_reg)
        _atomic_write_json(os.path.join(here, ".measure_registry.json"), m_reg)
        _atomic_write_json(os.path.join(here, ".column_registry.json"), c_reg)
    except Exception:
        pass

    print("\n  All metadata cached.")
    print(f"  Total metadata size: {len(metadata)} chars\n")
    return metadata
