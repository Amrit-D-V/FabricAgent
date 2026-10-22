from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import requests

from auth.token_manager import (
    FABRIC_SCOPE,
    PBI_SCOPE,
    _acquire_token,
    _write_file_cache,
    get_fabric_token,
    get_token_cached,
)

logger = logging.getLogger(__name__)

_PBI_BASE = "https://api.powerbi.com/v1.0/myorg"
_FABRIC_BASE = "https://api.fabric.microsoft.com/v1"
_TIMEOUT = 20

_last_error: str = ""


def get_last_error() -> str:
    return _last_error


def _set_error(msg: str) -> None:
    global _last_error
    _last_error = msg
    logger.warning(msg)


def _clear_error() -> None:
    global _last_error
    _last_error = ""


@dataclass
class Workspace:
    id: str
    name: str
    type: str = ""

    def __str__(self) -> str:
        return self.name


@dataclass
class SemanticModel:
    id: str
    name: str
    workspace_id: str
    workspace_name: str = ""
    configured_by: str = ""

    is_refreshable: bool = False
    xmla_endpoint: str = ""

    def __str__(self) -> str:
        return self.name


@dataclass
class DataSource:

    id: str
    name: str
    kind: str
    workspace_id: str
    workspace_name: str = ""
    connection_string: str = ""
    database_name: str = ""

    def __str__(self) -> str:
        return f"{self.name} ({self.kind})"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _is_token_expired_response(resp: requests.Response) -> bool:
    if resp.status_code not in (401, 403):
        return False
    try:
        body = resp.json()
        msg = json.dumps(body).lower()
    except Exception:
        msg = (resp.text or "").lower()
    return "tokenexpired" in msg or "token has expired" in msg or "expired_token" in msg


def _force_refresh(scope: str) -> str:
    try:
        from auth.token_manager import _token_cache

        for k in list(_token_cache.keys()):
            if scope.startswith(f"https://{k.replace('_', '.').split('_default')[0]}"):
                _token_cache.pop(k, None)
    except Exception:
        pass
    fresh = _acquire_token(scope)
    scope_key = scope.split("/")[2].replace(".", "_")
    _write_file_cache(scope_key, fresh)
    return fresh


def _get_with_refresh(url: str, scope: str, getter) -> requests.Response | None:
    for attempt in range(2):
        token = getter() if attempt == 0 else _force_refresh(scope)
        try:
            r = requests.get(url, headers=_bearer(token), timeout=_TIMEOUT)
        except Exception as e:
            _set_error(f"GET {url} transport error: {e}")
            return None
        if not _is_token_expired_response(r) or attempt == 1:
            return r
        logger.info(f"Token expired on {url}; refreshing and retrying once")
    return None


def _try_fabric_workspaces() -> list[Workspace]:
    r = _get_with_refresh(f"{_FABRIC_BASE}/workspaces", FABRIC_SCOPE, get_fabric_token)
    if r is None:
        return []
    try:
        r.raise_for_status()
    except requests.HTTPError as e:
        _set_error(
            f"Fabric REST /workspaces returned {e.response.status_code}: "
            f"{e.response.text[:200]}"
        )
        return []

    raw = r.json().get("value", [])
    out: list[Workspace] = []
    for w in raw:
        if not w.get("displayName") and not w.get("name"):
            continue
        out.append(
            Workspace(
                id=w.get("id", ""),
                name=w.get("displayName") or w.get("name", "(unnamed)"),
                type="Workspace",
            )
        )
    out.sort(key=lambda w: w.name.lower())
    if out:
        _clear_error()
    return out


def _try_pbi_workspaces() -> list[Workspace]:
    r = _get_with_refresh(f"{_PBI_BASE}/groups", PBI_SCOPE, get_token_cached)
    if r is None:
        return []
    try:
        r.raise_for_status()
    except requests.HTTPError as e:
        _set_error(
            f"PBI REST /groups returned {e.response.status_code}: "
            f"{e.response.text[:200]}"
        )
        return []

    raw = r.json().get("value", [])
    out: list[Workspace] = []
    for w in raw:
        if w.get("isReadOnly"):
            continue
        out.append(
            Workspace(
                id=w.get("id", ""),
                name=w.get("name", "(unnamed)"),
                type=w.get("type", "Workspace"),
            )
        )
    out.sort(key=lambda w: w.name.lower())
    if out:
        _clear_error()
    return out


def list_workspaces(token: str | None = None) -> list[Workspace]:
    out = _try_fabric_workspaces()
    if out:
        logger.info(f"list_workspaces (Fabric API): {len(out)} workspace(s)")
        return out

    out = _try_pbi_workspaces()
    if out:
        logger.info(f"list_workspaces (PBI API): {len(out)} workspace(s)")
        return out

    return []


def _try_fabric_semantic_models(
    workspace_id: str, workspace_name: str
) -> list[SemanticModel]:
    try:
        token = get_fabric_token()
        r = requests.get(
            f"{_FABRIC_BASE}/workspaces/{workspace_id}/semanticModels",
            headers=_bearer(token),
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
    except requests.HTTPError as e:
        _set_error(
            f"Fabric REST /semanticModels {e.response.status_code}: {e.response.text[:200]}"
        )
        return []
    except Exception as e:
        _set_error(f"Fabric REST /semanticModels error: {e}")
        return []

    raw = r.json().get("value", [])
    out: list[SemanticModel] = []
    for ds in raw:
        out.append(
            SemanticModel(
                id=ds.get("id", ""),
                name=ds.get("displayName") or ds.get("name", "(unnamed)"),
                workspace_id=workspace_id,
                workspace_name=workspace_name,
            )
        )
    out.sort(key=lambda m: m.name.lower())
    if out:
        _clear_error()
    return out


def _try_pbi_datasets(workspace_id: str, workspace_name: str) -> list[SemanticModel]:
    try:
        token = get_token_cached()
        r = requests.get(
            f"{_PBI_BASE}/groups/{workspace_id}/datasets",
            headers=_bearer(token),
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
    except requests.HTTPError as e:
        _set_error(
            f"PBI REST /datasets {e.response.status_code}: {e.response.text[:200]}"
        )
        return []
    except Exception as e:
        _set_error(f"PBI REST /datasets error: {e}")
        return []

    raw = r.json().get("value", [])
    out: list[SemanticModel] = []
    for ds in raw:
        out.append(
            SemanticModel(
                id=ds.get("id", ""),
                name=ds.get("name", "(unnamed)"),
                workspace_id=workspace_id,
                workspace_name=workspace_name,
                configured_by=ds.get("configuredBy", ""),
                is_refreshable=bool(ds.get("isRefreshable", False)),
            )
        )
    out.sort(key=lambda m: m.name.lower())
    if out:
        _clear_error()
    return out


def list_semantic_models(
    workspace_id: str,
    workspace_name: str = "",
    token: str | None = None,
) -> list[SemanticModel]:
    if not workspace_id:
        return []
    out = _try_fabric_semantic_models(workspace_id, workspace_name)
    if out:
        return out
    return _try_pbi_datasets(workspace_id, workspace_name)


def _fabric_get_items(workspace_id: str, item_type: str) -> list[dict]:
    r = _get_with_refresh(
        f"{_FABRIC_BASE}/workspaces/{workspace_id}/{item_type}",
        FABRIC_SCOPE,
        get_fabric_token,
    )
    if r is None:
        return []
    try:
        r.raise_for_status()
    except requests.HTTPError as e:
        _set_error(
            f"Fabric REST /{item_type} returned {e.response.status_code}: "
            f"{e.response.text[:200]}"
        )
        return []
    return r.json().get("value", [])


def list_warehouses(
    workspace_id: str,
    workspace_name: str = "",
) -> list[DataSource]:
    if not workspace_id:
        return []
    out: list[DataSource] = []
    for w in _fabric_get_items(workspace_id, "warehouses"):
        out.append(
            DataSource(
                id=w.get("id", ""),
                name=w.get("displayName") or w.get("name", "(unnamed)"),
                kind="Warehouse",
                workspace_id=workspace_id,
                workspace_name=workspace_name,
                connection_string=(
                    w.get("properties", {}).get("connectionString", "")
                    or w.get("connectionString", "")
                ),
            )
        )
    out.sort(key=lambda d: d.name.lower())
    if out:
        _clear_error()
    return out


def list_lakehouses(
    workspace_id: str,
    workspace_name: str = "",
) -> list[DataSource]:
    if not workspace_id:
        return []
    out: list[DataSource] = []
    for lh in _fabric_get_items(workspace_id, "lakehouses"):
        props = lh.get("properties", {})
        sql_ep = props.get("sqlEndpointProperties", {}) or {}
        out.append(
            DataSource(
                id=lh.get("id", ""),
                name=lh.get("displayName") or lh.get("name", "(unnamed)"),
                kind="Lakehouse",
                workspace_id=workspace_id,
                workspace_name=workspace_name,
                connection_string=sql_ep.get("connectionString", ""),
            )
        )
    out.sort(key=lambda d: d.name.lower())
    if out:
        _clear_error()
    return out


def list_sql_databases(
    workspace_id: str,
    workspace_name: str = "",
) -> list[DataSource]:
    if not workspace_id:
        return []
    out: list[DataSource] = []
    for db in _fabric_get_items(workspace_id, "sqlDatabases"):
        props = db.get("properties", {})
        out.append(
            DataSource(
                id=db.get("id", ""),
                name=db.get("displayName") or db.get("name", "(unnamed)"),
                kind="SQLDatabase",
                workspace_id=workspace_id,
                workspace_name=workspace_name,
                connection_string=props.get("serverFqdn", ""),
                database_name=props.get("databaseName", ""),
            )
        )
    out.sort(key=lambda d: d.name.lower())
    if out:
        _clear_error()
    return out


def list_kql_databases(
    workspace_id: str,
    workspace_name: str = "",
) -> list[DataSource]:
    if not workspace_id:
        return []
    out: list[DataSource] = []
    for db in _fabric_get_items(workspace_id, "kqlDatabases"):
        props = db.get("properties", {})
        out.append(
            DataSource(
                id=db.get("id", ""),
                name=db.get("displayName") or db.get("name", "(unnamed)"),
                kind="KQLDatabase",
                workspace_id=workspace_id,
                workspace_name=workspace_name,
                connection_string=props.get("queryServiceUri", ""),
            )
        )
    out.sort(key=lambda d: d.name.lower())
    if out:
        _clear_error()
    return out


def list_data_sources(
    workspace_id: str,
    workspace_name: str = "",
) -> list[DataSource]:
    out = (
        list_warehouses(workspace_id, workspace_name)
        + list_lakehouses(workspace_id, workspace_name)
        + list_sql_databases(workspace_id, workspace_name)
        + list_kql_databases(workspace_id, workspace_name)
    )
    seen: set[tuple[str, str]] = set()
    deduped: list[DataSource] = []
    for d in out:
        k = (d.kind, d.name.lower())
        if k in seen:
            continue
        seen.add(k)
        deduped.append(d)
    return deduped


def _matches(name: str, needle: str) -> bool:
    return not needle or needle.strip().lower() in (name or "").lower()


def _resolve_workspace(workspace: str) -> tuple[str, str, str]:
    """Name -> (id, name, error). Case-insensitive, since users type from memory."""
    if not workspace:
        return "", "", "ERROR - no workspace given and none is connected."
    wanted = workspace.strip().lower()
    spaces = list_workspaces()
    if not spaces:
        return (
            "",
            "",
            (
                f"ERROR - could not list workspaces, so '{workspace}' cannot be "
                f"resolved. {get_last_error() or 'Check the token permissions.'}"
            ),
        )
    for w in spaces:
        if w.name.strip().lower() == wanted:
            return w.id, w.name, ""
    near = [w.name for w in spaces if wanted in w.name.lower()][:10]
    hint = ("\nDid you mean: " + ", ".join(near)) if near else ""
    listing = ", ".join(sorted(w.name for w in spaces)[:25])
    return (
        "",
        "",
        (
            f"ERROR - no workspace named '{workspace}'.{hint}\nVisible workspaces: "
            f"{listing}"
        ),
    )


def _browse_reports(ws_name: str, name_filter: str, suffix: str) -> str:
    """Published reports in the workspace, as a text block for the assistant.

    These come from the Power BI REST endpoint /groups/{id}/reports — the
    Fabric items API behind list_semantic_models does not surface reports,
    which is why "what reports are in this workspace?" used to be
    unanswerable. Distinct from pbi_reports_list, which scans LOCAL .pbip
    projects on disk.
    """
    try:
        from fabric.fabric_connection import get_fabric_connection
        from fabric.report_fetch import _PBI_SCOPE, list_reports, token

        conn = get_fabric_connection()
        reports = [
            r
            for r in list_reports(conn, token(conn, _PBI_SCOPE))
            if _matches(r.get("name") or "", name_filter)
        ]
    except Exception as exc:
        return f"\nCould not list reports in '{ws_name}': {exc}"

    if not reports:
        return f"\nNo reports in '{ws_name}'{suffix}."
    lines = [f"\nReports in '{ws_name}' ({len(reports)}){suffix}:"]
    for r in reports:
        ds = r.get("datasetId") or ""
        lines.append(
            f"  - {r.get('name') or '?'}  [id {r.get('id')}]"
            + (f"  dataset {ds}" if ds else "")
        )
    return "\n".join(lines)


def browse(
    kind: str = "semantic_models",
    workspace: str = "",
    name_filter: str = "",
) -> str:
    """Text listing of what lives in a workspace, for the assistant to read.

    This is the discovery path that only the Streamlit connection popover used
    to have. Without it the assistant could describe the one model it was
    connected to but could not answer "what else is in here?".
    """
    kind = (kind or "semantic_models").strip().lower()
    suffix = f" matching '{name_filter}'" if name_filter else ""

    if kind == "workspaces":
        spaces = [w for w in list_workspaces() if _matches(w.name, name_filter)]
        if not spaces:
            return (
                f"No workspaces found{suffix}. "
                f"{get_last_error() or 'The token may lack tenant visibility.'}"
            )
        lines = [f"Workspaces ({len(spaces)}){suffix}:"]
        lines += [f"  - {w.name}  [id {w.id}]" for w in spaces]
        return "\n".join(lines)

    ws_id, ws_name, err = _resolve_workspace(workspace)
    if err:
        return err

    out: list[str] = []

    if kind in ("semantic_models", "all"):
        models = [
            m
            for m in list_semantic_models(ws_id, ws_name)
            if _matches(m.name, name_filter)
        ]
        if models:
            out.append(f"Semantic models in '{ws_name}' ({len(models)}){suffix}:")
            for m in models:
                # Only the PBI fallback populates configured_by/is_refreshable;
                # the Fabric endpoint leaves them at their defaults. Printing
                # "not refreshable" for an unset default would state as fact
                # something we never learned, so show owner only when present
                # and say nothing about refreshability at all.
                owner = f"  owner {m.configured_by}" if m.configured_by else ""
                out.append(f"  - {m.name}  [id {m.id}]{owner}")
        else:
            out.append(f"No semantic models in '{ws_name}'{suffix}.")

    if kind in ("data_sources", "all"):
        sources = [
            d
            for d in list_data_sources(ws_id, ws_name)
            if _matches(d.name, name_filter)
        ]
        if sources:
            out.append(f"\nData sources in '{ws_name}' ({len(sources)}){suffix}:")
            for d in sources:
                where = f"  {d.connection_string}" if d.connection_string else ""
                out.append(f"  - {d.name} ({d.kind}) [id {d.id}]{where}")
        else:
            out.append(f"\nNo warehouses/lakehouses/SQL DBs in '{ws_name}'{suffix}.")

    if kind in ("reports", "all"):
        out.append(_browse_reports(ws_name, name_filter, suffix))

    if not out:
        return (
            f"ERROR - unknown kind '{kind}'. Use one of: semantic_models, "
            f"data_sources, reports, workspaces, all."
        )
    return "\n".join(out)
