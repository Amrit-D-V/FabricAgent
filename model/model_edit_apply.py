from __future__ import annotations

import asyncio
import json
import os
import sys


def _as_dict(definition) -> dict:
    for _ in range(2):
        if isinstance(definition, dict):
            return definition
        if isinstance(definition, str) and definition.strip():
            try:
                definition = json.loads(definition)
            except (ValueError, TypeError):
                return {}
        else:
            return {}
    return definition if isinstance(definition, dict) else {}


from semantic_model_v2.mcp_apply import (
    _begin_transaction,
    _commit_transaction,
    _is_error_result,
    _mcp_imports,
    _parse_mcp_json,
    _rollback_transaction,
)


def _conn_name(workspace: str, model_name: str) -> str:
    return f"Fabric-{workspace}-{model_name}"


def build_create_request(edit: dict, conn_name: str) -> tuple[str, dict]:
    otype = edit.get("object_type")
    name = edit.get("name", "")
    table = edit.get("table", "")
    definition = edit.get("definition", "")
    if otype == "measure":
        meas_def: dict = {"name": name, "tableName": table, "expression": definition}
        if edit.get("format_string"):
            meas_def["formatString"] = edit["format_string"]
        if edit.get("display_folder"):
            meas_def["displayFolder"] = edit["display_folder"]
        return "measure_operations", {
            "request": {
                "operation": "Create",
                "connectionName": conn_name,
                "definitions": [meas_def],
            }
        }
    if otype == "column":
        col_def: dict = {"tableName": table, "name": name, "expression": definition}
        if edit.get("data_type"):
            col_def["dataType"] = edit["data_type"]
        if edit.get("format_string"):
            col_def["formatString"] = edit["format_string"]
        if edit.get("display_folder"):
            col_def["displayFolder"] = edit["display_folder"]
        return "column_operations", {
            "request": {
                "operation": "Create",
                "connectionName": conn_name,
                "definitions": [col_def],
            }
        }
    if otype == "relationship":
        rel_def = _as_dict(definition)
        return "relationship_operations", {
            "request": {
                "operation": "Create",
                "connectionName": conn_name,
                "definitions": [rel_def],
            }
        }
    if otype == "calculation_group":
        grp_def = dict(_as_dict(definition))
        grp_def["name"] = name
        return "calculation_group_operations", {
            "request": {
                "operation": "CreateGroup",
                "connectionName": conn_name,
                "groupDefinitions": [grp_def],
            }
        }
    raise ValueError(f"unknown object_type: {otype!r}")


def build_update_request(edit: dict, conn_name: str) -> tuple[str, dict]:
    otype = edit.get("object_type")
    name = edit.get("name", "")
    table = edit.get("table", "")
    definition = edit.get("definition", "")
    if otype == "measure":
        upd: dict = {}
        if definition:
            upd["expression"] = definition
        if edit.get("format_string"):
            upd["formatString"] = edit["format_string"]
        if edit.get("display_folder"):
            upd["displayFolder"] = edit["display_folder"]
        # Update targets the object by name/tableName inside the definition,
        # exactly like Create -- there is no request-level measureName.
        upd["name"] = name
        if table:
            upd["tableName"] = table
        return "measure_operations", {
            "request": {
                "operation": "Update",
                "connectionName": conn_name,
                "definitions": [upd],
            }
        }
    if otype == "column":
        upd = {}
        if definition:
            upd["expression"] = definition
        if edit.get("data_type"):
            upd["dataType"] = edit["data_type"]
        if edit.get("format_string"):
            upd["formatString"] = edit["format_string"]
        if edit.get("display_folder"):
            upd["displayFolder"] = edit["display_folder"]
        upd["name"] = name
        if table:
            upd["tableName"] = table
        return "column_operations", {
            "request": {
                "operation": "Update",
                "connectionName": conn_name,
                "definitions": [upd],
            }
        }
    if otype == "relationship":
        rel_def = _as_dict(definition)
        rel_def.setdefault("name", name)
        return "relationship_operations", {
            "request": {
                "operation": "Update",
                "connectionName": conn_name,
                "definitions": [rel_def],
            }
        }
    raise ValueError(f"cannot update object_type: {otype!r}")


def build_edit_request(edit: dict, conn_name: str) -> tuple[str, dict]:
    if (edit.get("operation") or "create") == "update":
        return build_update_request(edit, conn_name)
    return build_create_request(edit, conn_name)


def build_delete_request(obj: dict, conn_name: str) -> tuple[str, dict]:
    otype = obj.get("type") or obj.get("object_type")
    name = obj.get("name", "")
    table = obj.get("table", "")
    # Delete addresses objects through `references` -- a list of
    # {name, tableName} -- not request-level *Name fields.
    if otype == "measure":
        _ref: dict = {"name": name}
        if table:
            _ref["tableName"] = table
        return "measure_operations", {
            "request": {
                "operation": "Delete",
                "connectionName": conn_name,
                "references": [_ref],
            }
        }
    if otype == "column":
        _ref = {"name": name}
        if table:
            _ref["tableName"] = table
        return "column_operations", {
            "request": {
                "operation": "Delete",
                "connectionName": conn_name,
                "references": [_ref],
                "shouldCascadeDelete": True,
            }
        }
    if otype == "relationship":
        return "relationship_operations", {
            "request": {
                "operation": "Delete",
                "connectionName": conn_name,
                "references": [{"name": name}],
            }
        }
    if otype == "calculation_group":
        return "calculation_group_operations", {
            "request": {
                "operation": "DeleteGroup",
                "connectionName": conn_name,
                "groupReferences": [{"name": name}],
            }
        }
    raise ValueError(f"unknown object type: {otype!r}")


def _resolve_session_params(token, mcp_server_path):
    from fabric.fabric_connection import mcp_launch_command
    from fabric.model_metadata import _mcp_env

    _cmd, _args = mcp_launch_command()
    explicit = mcp_server_path or os.getenv("MCP_SERVER_PATH", "")
    if explicit:
        _cmd = explicit
    if _cmd not in ("npx", "npx.cmd") and (not _cmd or not os.path.isfile(_cmd)):
        return None, f"MCP server not found: {_cmd or '(unset)'}"

    env = _mcp_env(token or "")
    if not env.get("AZURE_TOKEN_CREDENTIALS") and not env.get(
        "PBI_MODELING_MCP_ACCESS_TOKEN"
    ):
        return (
            None,
            "No service-principal credentials or access token for the modeling MCP server.",
        )

    _, StdioServerParameters, _ = _mcp_imports()
    return StdioServerParameters(command=_cmd, args=_args, env=env), None


async def _connect_fabric(session, workspace, model_name):
    connect = await session.call_tool(
        "connection_operations",
        {
            "request": {
                "operation": "ConnectFabric",
                "workspaceName": workspace,
                "semanticModelName": model_name,
            }
        },
    )
    err = _result_error(connect)
    return f"ConnectFabric failed: {err}" if err else None


async def _call_in_session(workspace, model_name, token, mcp_server_path, calls):
    params, err = _resolve_session_params(token, mcp_server_path)
    if err:
        return False, err

    ClientSession, _, stdio_client = _mcp_imports()
    conn_name = _conn_name(workspace, model_name)
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        cerr = await _connect_fabric(session, workspace, model_name)
        if cerr:
            return False, cerr
        return await _run_edit_ops(session, conn_name, calls)


async def _apply_batch_in_session(workspace, model_name, token, mcp_server_path, edits):
    params, err = _resolve_session_params(token, mcp_server_path)
    if err:
        return [(e, False, err) for e in edits]

    ClientSession, _, stdio_client = _mcp_imports()
    conn_name = _conn_name(workspace, model_name)
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        cerr = await _connect_fabric(session, workspace, model_name)
        if cerr:
            return [(e, False, cerr) for e in edits]
        results = []
        for e in edits:
            try:
                tool, args = build_edit_request(e, conn_name)
                res = await session.call_tool(tool, args)
                op_err = _result_error(res)
                results.append((e, op_err is None, op_err or "ok"))
            except Exception as ex:
                results.append((e, False, str(ex)))
        return results


async def _run_edit_ops(session, conn_name, calls) -> tuple[bool, str]:
    txid = await _begin_transaction(session, conn_name)
    try:
        for tool, args in calls:
            res = await session.call_tool(tool, args)
            err = _result_error(res)
            if err:
                if txid:
                    ok, rb_msg = await _rollback_transaction(session, txid)
                    suffix = "" if ok else f" (rollback also errored: {rb_msg})"
                    return False, f"{err}; rolled back already-applied ops{suffix}"
                return False, err
        if txid:
            ok, msg = await _commit_transaction(session, txid)
            if not ok:
                return False, f"commit failed: {msg}"
        return True, "ok"
    except Exception as e:
        if txid:
            try:
                await _rollback_transaction(session, txid)
            except Exception:
                pass
        return False, str(e)


def _raw_text(result) -> str:
    c = getattr(result, "content", result)
    if isinstance(c, list):
        return " ".join(str(getattr(x, "text", x)) for x in c)
    return str(c)


def _result_error(result) -> str | None:
    if getattr(result, "isError", False):
        return _raw_text(result)[:300] or "tool reported isError"
    parsed = _parse_mcp_json(result)
    if parsed is not None:
        is_err, msg = _is_error_result(parsed)
        if is_err:
            return msg
    return None


def _run(coro):
    loop = (
        asyncio.ProactorEventLoop()
        if sys.platform == "win32"
        else asyncio.new_event_loop()
    )
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def apply_model_edit(
    edit: dict,
    workspace: str,
    model_name: str,
    token: str,
    mcp_server_path: str | None = None,
) -> tuple[bool, str]:
    conn = _conn_name(workspace, model_name)
    tool, args = build_edit_request(edit, conn)
    return _run(
        _call_in_session(workspace, model_name, token, mcp_server_path, [(tool, args)])
    )


def is_already_exists(msg: str) -> bool:
    return "already exist" in (msg or "").lower()


def edit_key(e: dict) -> tuple:
    return (
        (e.get("operation") or "create"),
        e.get("object_type"),
        (e.get("table") or "").strip().lower(),
        (e.get("name") or "").strip().lower(),
    )


def dedupe_edits(edits: list[dict]) -> list[dict]:
    by_key = {edit_key(e): e for e in edits}  # last write wins
    seen: set = set()
    out: list[dict] = []
    for e in edits:
        key = edit_key(e)
        if key in seen:
            continue
        seen.add(key)
        out.append(by_key[key])
    return out


def apply_model_edits(
    edits: list[dict],
    workspace: str,
    model_name: str,
    token: str,
    mcp_server_path: str | None = None,
) -> list[tuple[dict, bool, str]]:
    edits = dedupe_edits(edits)
    if not edits:
        return []
    return _run(
        _apply_batch_in_session(workspace, model_name, token, mcp_server_path, edits)
    )


def cleanup_objects(
    objs: list[dict],
    workspace: str,
    model_name: str,
    token: str,
    mcp_server_path: str | None = None,
) -> tuple[int, list[str]]:
    conn = _conn_name(workspace, model_name)
    calls = [build_delete_request(o, conn) for o in objs]
    if not calls:
        return 0, []
    ok, msg = _run(
        _call_in_session(workspace, model_name, token, mcp_server_path, calls)
    )
    return (len(calls), []) if ok else (0, [msg])
