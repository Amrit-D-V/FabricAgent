from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


def _mcp_imports():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    return ClientSession, StdioServerParameters, stdio_client


@dataclass
class MCPOperationResult:

    op: str
    succeeded: bool
    message: str = ""
    raw: dict | None = None


@dataclass
class MCPApplyReport:

    operations: list[MCPOperationResult] = field(default_factory=list)
    connected: bool = False
    connection_error: str = ""
    verified: bool = False
    notes: list[str] = field(default_factory=list)
    _forced_succeeded: bool | None = None

    @property
    def succeeded(self) -> bool:
        if self._forced_succeeded is not None:
            return self._forced_succeeded
        return self.connected and all(op.succeeded for op in self.operations)

    @property
    def succeeded_count(self) -> int:
        return sum(1 for op in self.operations if op.succeeded)

    @property
    def failed_count(self) -> int:
        return sum(1 for op in self.operations if not op.succeeded)

    def summary(self) -> str:
        if not self.connected:
            return f"MCP connect failed: {self.connection_error}"
        if not self.operations:
            return "No operations to apply"
        return (
            f"{self.succeeded_count}/{len(self.operations)} operations succeeded "
            f"({self.failed_count} failed)"
        )

    def detail_lines(self) -> list[str]:
        if not self.connected:
            return [f"[ERROR] {self.connection_error}"]
        lines = [
            f"[{'OK' if op.succeeded else 'FAIL'}] {op.op}"
            + (f": {op.message}" if op.message else "")
            for op in self.operations
        ]
        lines.extend(f"[NOTE] {n}" for n in self.notes)
        return lines

    def _force(self, value: bool) -> None:
        self._forced_succeeded = value


def _parse_mcp_json(result) -> dict | None:
    if isinstance(result, dict):
        return result
    content = getattr(result, "content", None)
    blocks = content if isinstance(content, list) else [content]
    for item in blocks:
        text = getattr(item, "text", None)
        if text is None and isinstance(item, str):
            text = item
        if not text:
            continue
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            s, e = text.find("{"), text.rfind("}")
            if 0 <= s < e:
                try:
                    return json.loads(text[s : e + 1])
                except json.JSONDecodeError:
                    continue
    return None


def _is_error_result(parsed: dict | None) -> tuple[bool, str]:
    if not parsed:
        return True, "no parsed response"
    if isinstance(parsed, dict):
        if parsed.get("error"):
            return True, str(parsed["error"])
        if parsed.get("success") is False:
            return True, str(
                parsed.get("message") or "operation returned success=False"
            )
        if parsed.get("isError"):
            return True, str(parsed.get("message") or "isError=True")
    return False, ""


_TXID_KEYS = ("transactionId", "TransactionId", "transactionID", "id", "Id", "ID")


def _extract_transaction_id(parsed: dict | None) -> str | None:
    if not isinstance(parsed, dict):
        return None
    for key in _TXID_KEYS:
        val = parsed.get(key)
        if val:
            return str(val)
    for container in ("data", "result", "transaction"):
        nested = parsed.get(container)
        if isinstance(nested, dict):
            for key in _TXID_KEYS:
                val = nested.get(key)
                if val:
                    return str(val)
    return None


async def _begin_transaction(session, conn_name: str) -> str | None:
    res = await session.call_tool(
        "transaction_operations",
        {
            "request": {"operation": "Begin", "connectionName": conn_name},
        },
    )
    parsed = _parse_mcp_json(res)
    is_err, err_msg = _is_error_result(parsed)
    if is_err:
        logger.warning(f"transaction Begin failed on {conn_name}: {err_msg}")
        return None
    txid = _extract_transaction_id(parsed)
    if not txid:
        logger.warning(
            f"transaction Begin on {conn_name} returned no transaction id "
            f"(parsed={parsed!r})"
        )
        return None
    return txid


async def _commit_transaction(session, txid: str) -> tuple[bool, str]:
    res = await session.call_tool(
        "transaction_operations",
        {
            "request": {"operation": "Commit", "transactionId": txid},
        },
    )
    parsed = _parse_mcp_json(res)
    is_err, err_msg = _is_error_result(parsed)
    return (not is_err), err_msg


async def _rollback_transaction(session, txid: str) -> tuple[bool, str]:
    res = await session.call_tool(
        "transaction_operations",
        {
            "request": {"operation": "Rollback", "transactionId": txid},
        },
    )
    parsed = _parse_mcp_json(res)
    is_err, err_msg = _is_error_result(parsed)
    return (not is_err), err_msg


def _names_from_list(parsed: dict | None, name_keys: tuple[str, ...]) -> set[str]:
    candidates: list = []
    if isinstance(parsed, list):
        candidates = parsed
    elif isinstance(parsed, dict):
        for key in (
            "measures",
            "Measures",
            "tables",
            "Tables",
            "data",
            "items",
            "value",
        ):
            val = parsed.get(key)
            if isinstance(val, list):
                candidates = val
                break
            if isinstance(val, dict) and isinstance(val.get("rows"), list):
                candidates = val["rows"]
                break
    names: set[str] = set()
    for entry in candidates:
        if isinstance(entry, str):
            names.add(entry)
        elif isinstance(entry, dict):
            for nk in name_keys:
                if entry.get(nk):
                    names.add(str(entry[nk]))
                    break
    return names


def _measure_names_from_list(parsed: dict | None) -> set[str]:
    return _names_from_list(parsed, ("name", "Name", "measureName", "[Name]"))


def _table_names_from_list(parsed: dict | None) -> set[str]:
    return _names_from_list(parsed, ("name", "Name", "tableName", "table", "[Name]"))


async def _list_measure_names(
    session, conn_name: str, table_name: str | None = None
) -> set[str]:
    req = {"operation": "List", "connectionName": conn_name}
    if table_name:
        req["tableName"] = table_name
    res = await session.call_tool("measure_operations", {"request": req})
    return _measure_names_from_list(_parse_mcp_json(res))


async def _verify_patch(
    session, conn_name: str, patch_dict: dict, report: MCPApplyReport
) -> bool:
    adds = [(e.get("name") or "").strip() for e in patch_dict.get("add_measures", [])]
    adds = [n for n in adds if n]
    renames = [
        ((e.get("from") or "").strip(), (e.get("to") or "").strip())
        for e in patch_dict.get("rename_measures", [])
    ]
    renames = [(f, t) for f, t in renames if f and t]
    drops = [(e.get("name") or "").strip() for e in patch_dict.get("drop_measures", [])]
    drops = [n for n in drops if n]

    add_tables = {
        (e.get("host_table") or "").strip() for e in patch_dict.get("add_measures", [])
    }
    add_tables.discard("")
    table_filter = None
    if len(add_tables) == 1 and not renames and not drops:
        table_filter = next(iter(add_tables))

    present = await _list_measure_names(session, conn_name, table_filter)

    ok = True
    for name in adds:
        if name in present:
            report.notes.append(f"verified add: '{name}' present")
        else:
            ok = False
            report.notes.append(f"VERIFY FAIL: added measure '{name}' not found")
    for old, new in renames:
        if new in present and old not in present:
            report.notes.append(f"verified rename: '{old}' -> '{new}'")
        else:
            ok = False
            report.notes.append(
                f"VERIFY FAIL: rename '{old}' -> '{new}' "
                f"(to_present={new in present}, from_absent={old not in present})"
            )
    for name in drops:
        if name not in present:
            report.notes.append(f"verified drop: '{name}' absent")
        else:
            ok = False
            report.notes.append(f"VERIFY FAIL: dropped measure '{name}' still present")

    calc_tables = [
        (e.get("name") or "").strip()
        for e in patch_dict.get("add_tables", [])
        if (e.get("kind") or "").strip() == "calculated"
        and (e.get("name") or "").strip()
    ]
    if calc_tables:
        try:
            tbl_res = await session.call_tool(
                "table_operations",
                {
                    "request": {"operation": "List", "connectionName": conn_name},
                },
            )
            tbl_parsed = _parse_mcp_json(tbl_res)
            tbl_names = _table_names_from_list(tbl_parsed)
            for name in calc_tables:
                if name in tbl_names:
                    report.notes.append(f"verified calculated table: '{name}' present")
                else:
                    report.notes.append(
                        f"calculated table '{name}' not confirmed via List (not-verified)"
                    )
        except Exception as e:
            report.notes.append(f"calculated-table verification skipped: {e}")

    return ok


async def _apply_patch_async(
    patch_dict: dict,
    workspace: str,
    model_name: str,
    access_token: str | None = None,
    mcp_server_path: str | None = None,
) -> MCPApplyReport:
    report = MCPApplyReport()

    from config import MCP_SERVER_PATH as _DEFAULT_MCP_SERVER_PATH
    from fabric.fabric_connection import mcp_launch_command

    _cmd, _args = mcp_launch_command(["--start", "--skipconfirmation"])
    explicit = mcp_server_path or _DEFAULT_MCP_SERVER_PATH
    if explicit:
        _cmd = explicit
    if _cmd not in ("npx", "npx.cmd") and (not _cmd or not os.path.isfile(_cmd)):
        report.connection_error = (
            f"MCP_SERVER_PATH not found: {_cmd or '(unset)'}. "
            f"Install the Power BI Modeling MCP extension first."
        )
        return report

    token = access_token or os.getenv("PBI_MODELING_MCP_ACCESS_TOKEN", "")
    if not token:
        report.connection_error = (
            "No access token. Provide one or set PBI_MODELING_MCP_ACCESS_TOKEN."
        )
        return report

    try:
        ClientSession, StdioServerParameters, stdio_client = _mcp_imports()
    except ImportError as e:
        report.connection_error = f"mcp package not installed: {e}"
        return report

    server_params = StdioServerParameters(
        command=_cmd,
        args=_args,
        env={**os.environ, "PBI_MODELING_MCP_ACCESS_TOKEN": token},
    )
    conn_name = f"Fabric-{workspace}-{model_name}"

    try:
        async with (
            stdio_client(server_params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()

            connect_res = await session.call_tool(
                "connection_operations",
                {
                    "request": {
                        "operation": "ConnectFabric",
                        "workspaceName": workspace,
                        "semanticModelName": model_name,
                    },
                },
            )
            parsed = _parse_mcp_json(connect_res)
            is_err, err_msg = _is_error_result(parsed)
            if is_err:
                report.connection_error = (
                    f"ConnectFabric failed for {workspace}/{model_name}: {err_msg}"
                )
                return report
            report.connected = True
            logger.info(f"MCP connected to {workspace}/{model_name}")

            ops_report = await _run_patch_ops(session, conn_name, patch_dict)
            ops_report.connected = True
            return ops_report

    except Exception as e:
        if not report.connected:
            report.connection_error = f"MCP transport error: {e}"
        else:
            report.operations.append(
                MCPOperationResult(
                    op="(post-connect)",
                    succeeded=False,
                    message=str(e),
                )
            )

    return report


async def _run_patch_ops(session, conn_name: str, patch_dict: dict) -> MCPApplyReport:
    report = MCPApplyReport()

    txid = await _begin_transaction(session, conn_name)
    if not txid:
        report.notes.append(
            "no explicit transaction (Begin unsupported on this connection); "
            "applying without rollback safety"
        )

    try:
        await _execute_ops(session, conn_name, patch_dict, report)

        any_failed = any(not op.succeeded for op in report.operations)

        if any_failed:
            if txid:
                ok, msg = await _rollback_transaction(session, txid)
                report.notes.append(
                    "rolled back: one or more operations failed"
                    + ("" if ok else f" (rollback also errored: {msg})")
                )
            else:
                report.notes.append(
                    "one or more operations failed (no transaction to roll back — "
                    "already-applied ops remain)"
                )
            report._force(False)
            return report

        report.verified = await _verify_patch(session, conn_name, patch_dict, report)
        if not report.verified:
            if txid:
                ok, msg = await _rollback_transaction(session, txid)
                report.notes.append(
                    "rolled back: read-back verification failed"
                    + ("" if ok else f" (rollback also errored: {msg})")
                )
            else:
                report.notes.append(
                    "read-back verification failed (no transaction to roll back)"
                )
            report._force(False)
            return report

        if txid:
            ok, msg = await _commit_transaction(session, txid)
            if not ok:
                report.notes.append(f"commit failed: {msg}")
                report._force(False)
                return report
            report.notes.append("transaction committed")
        report._force(True)

        if report.operations:
            try:
                await session.call_tool(
                    "model_operations",
                    {
                        "request": {
                            "operation": "Refresh",
                            "connectionName": conn_name,
                        },
                    },
                )
            except Exception as e:
                logger.info(f"Model refresh skipped: {e}")
        return report

    except Exception as e:
        if txid:
            try:
                ok, msg = await _rollback_transaction(session, txid)
                report.notes.append(
                    f"exception during ops ({e}); rolled back"
                    + ("" if ok else f" (rollback also errored: {msg})")
                )
            except Exception as re:
                report.notes.append(
                    f"exception during ops ({e}); rollback also raised: {re}"
                )
        else:
            report.notes.append(f"exception during ops ({e})")
        report.operations.append(
            MCPOperationResult(
                op="(transactional run)",
                succeeded=False,
                message=str(e),
            )
        )
        report._force(False)
        return report


async def _execute_ops(
    session, conn_name: str, patch_dict: dict, report: MCPApplyReport
) -> None:
    for entry in patch_dict.get("add_measures", []):
        name = (entry.get("name") or "").strip()
        host = (entry.get("host_table") or "").strip()
        dax = (entry.get("dax") or "").strip()
        if not (name and host and dax):
            report.operations.append(
                MCPOperationResult(
                    op=f"Create measure '{name}'",
                    succeeded=False,
                    message="missing name/host/dax",
                )
            )
            continue
        _meas_def = {"name": name, "tableName": host, "expression": dax}
        if entry.get("format_string"):
            _meas_def["formatString"] = entry["format_string"]
        if entry.get("display_folder"):
            _meas_def["displayFolder"] = entry["display_folder"]
        create_res = await session.call_tool(
            "measure_operations",
            {
                "request": {
                    "operation": "Create",
                    "connectionName": conn_name,
                    "tableName": host,
                    "measureName": name,
                    "createDefinition": _meas_def,
                },
            },
        )
        parsed = _parse_mcp_json(create_res)
        is_err, err_msg = _is_error_result(parsed)
        report.operations.append(
            MCPOperationResult(
                op=f"Create measure '{name}' on {host}",
                succeeded=not is_err,
                message=err_msg,
                raw=parsed,
            )
        )

    for entry in patch_dict.get("drop_measures", []):
        name = (entry.get("name") or "").strip()
        if not name:
            continue
        drop_res = await session.call_tool(
            "measure_operations",
            {
                "request": {
                    "operation": "Delete",
                    "connectionName": conn_name,
                    "measureName": name,
                },
            },
        )
        parsed = _parse_mcp_json(drop_res)
        is_err, err_msg = _is_error_result(parsed)
        report.operations.append(
            MCPOperationResult(
                op=f"Delete measure '{name}'",
                succeeded=not is_err,
                message=err_msg,
                raw=parsed,
            )
        )

    for entry in patch_dict.get("rename_measures", []):
        old = (entry.get("from") or "").strip()
        new = (entry.get("to") or "").strip()
        if not (old and new):
            continue
        rn_res = await session.call_tool(
            "measure_operations",
            {
                "request": {
                    "operation": "Rename",
                    "connectionName": conn_name,
                    "measureName": old,
                    "newName": new,
                },
            },
        )
        parsed = _parse_mcp_json(rn_res)
        is_err, err_msg = _is_error_result(parsed)
        report.operations.append(
            MCPOperationResult(
                op=f"Rename measure '{old}' -> '{new}'",
                succeeded=not is_err,
                message=err_msg,
                raw=parsed,
            )
        )

    for entry in patch_dict.get("add_tables", []):
        name = (entry.get("name") or "").strip()
        kind = (entry.get("kind") or "").strip()
        if not name:
            continue
        if kind != "calculated":
            report.operations.append(
                MCPOperationResult(
                    op=f"Create table '{name}'",
                    succeeded=False,
                    message=(
                        f"only calculated tables can be added via MCP "
                        f"in this module (got kind={kind!r}); use the "
                        f"file-writing pipeline for source-backed tables"
                    ),
                )
            )
            continue
        dax_expr = entry.get("dax_expression") or 'ROW("_dummy", 1)'
        tbl_res = await session.call_tool(
            "table_operations",
            {
                "request": {
                    "operation": "CreateCalculated",
                    "connectionName": conn_name,
                    "tableName": name,
                    "expression": dax_expr,
                },
            },
        )
        parsed = _parse_mcp_json(tbl_res)
        is_err, err_msg = _is_error_result(parsed)
        report.operations.append(
            MCPOperationResult(
                op=f"Create calculated table '{name}'",
                succeeded=not is_err,
                message=err_msg,
                raw=parsed,
            )
        )


def apply_patch_via_mcp(
    patch_dict: dict,
    workspace: str,
    model_name: str,
    access_token: str | None = None,
    mcp_server_path: str | None = None,
) -> MCPApplyReport:
    return asyncio.run(
        _apply_patch_async(
            patch_dict=patch_dict,
            workspace=workspace,
            model_name=model_name,
            access_token=access_token,
            mcp_server_path=mcp_server_path,
        )
    )
