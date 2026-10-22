from __future__ import annotations

import os
from dataclasses import dataclass, field

from config import (
    AZURE_CLIENT_ID,
    AZURE_CLIENT_SECRET,
    AZURE_TENANT_ID,
    FABRIC_MODELS,
    FABRIC_WORKSPACE,
    FABRIC_WORKSPACE_ID,
    MCP_SERVER_PATH,
    PBI_PAGINATED_CATALOGS,
    XMLA_ENDPOINT_BASE,
)

_cache: FabricConnection | None = None

# Which project's Fabric account is currently in force (None = default entry).
_active_project_id: int | None = None

# workspace id -> current name, so a Fabric rename doesn't break saved
# connections. Cleared alongside the connection cache.
_WS_NAME_BY_ID: dict[str, str] = {}


@dataclass(frozen=True)
class FabricConnection:
    tenant_id: str
    client_id: str
    client_secret: str
    workspace: str
    workspace_id: str
    models: tuple[str, ...] = ()
    catalogs: dict[str, str] = field(default_factory=dict)
    source: str = "env"
    name: str = ""  # library entry name, e.g. "ClientCo Fabric"

    @property
    def server(self) -> str:
        return f"{XMLA_ENDPOINT_BASE}/{self.workspace}" if self.workspace else ""

    def live_workspace(self) -> str:
        """The workspace's CURRENT name, resolved from its stable ID.

        Workspaces can be renamed in Fabric; the ID never changes. XMLA
        addresses a workspace by name, so a rename silently breaks every
        connection with "The specified Power BI workspace ('<old>') is not
        found" — which Power BI reports for an invisible workspace, making it
        look like a permissions problem. Resolving by ID keeps a saved
        connection working across renames. Falls back to the stored name.
        """
        if not self.workspace_id or not self.has_sp:
            return self.workspace
        cached = _WS_NAME_BY_ID.get(self.workspace_id)
        if cached:
            return cached
        try:
            from fabric.powerbi_rest import list_workspaces, sp_powerbi_token

            token = sp_powerbi_token(self.tenant_id, self.client_id, self.client_secret)
            for name, wid in list_workspaces(token):
                if wid:
                    _WS_NAME_BY_ID[wid.lower()] = name
                    _WS_NAME_BY_ID[wid] = name
        except Exception as exc:
            import logging

            logging.getLogger(__name__).debug(
                "workspace-name resolution by ID failed, using stored name: %s", exc
            )
            return self.workspace
        return _WS_NAME_BY_ID.get(self.workspace_id) or self.workspace

    @property
    def has_sp(self) -> bool:
        return all([self.tenant_id, self.client_id, self.client_secret])


def _from_env() -> FabricConnection:
    return FabricConnection(
        tenant_id=AZURE_TENANT_ID,
        client_id=AZURE_CLIENT_ID,
        client_secret=AZURE_CLIENT_SECRET,
        workspace=FABRIC_WORKSPACE,
        workspace_id=FABRIC_WORKSPACE_ID,
        models=tuple(FABRIC_MODELS),
        catalogs=dict(PBI_PAGINATED_CATALOGS),
        source="env",
    )


def set_active_project(project_id: int | None) -> None:
    """Point the connection resolver at a project (its bound Fabric account).

    app.py calls this when the active project changes. Connections live in a
    library (`fabric_connections`); each project binds to one and anything
    unbound falls back to the default entry, then to .env.
    """
    global _active_project_id, _cache
    if project_id != _active_project_id:
        _active_project_id = project_id
        _cache = None
        _WS_NAME_BY_ID.clear()


def get_active_project() -> int | None:
    return _active_project_id


def get_fabric_connection(project_id: int | None = None) -> FabricConnection:
    """The Fabric account in force — for `project_id`, else the active project.

    Callers that pass nothing keep the historic behaviour (the active
    connection), so every existing call site is unaffected.
    """
    global _cache
    _pid = _active_project_id if project_id is None else project_id
    if project_id is None and _cache is not None:
        return _cache
    try:
        from db import database

        row = database.resolve_fabric_connection(_pid)
        if row is None:  # library empty — legacy single-row table
            row = database.get_fabric_connection_row()
    except Exception as exc:
        import logging

        logging.getLogger(__name__).debug(
            "fabric DB read failed, using .env fallback: %s", exc
        )
        row = None
    if row:
        conn = FabricConnection(
            tenant_id=row["tenant_id"],
            client_id=row["client_id"],
            client_secret=row["client_secret"],
            workspace=row["workspace_name"],
            workspace_id=row["workspace_id"],
            models=tuple(row["models"]),
            catalogs=dict(row["catalogs"]),
            source="db",
            name=row.get("name", ""),
        )
    else:
        conn = _from_env()
    if project_id is None:
        _cache = conn
    return conn


def clear_fabric_connection_cache() -> None:
    global _cache
    _cache = None
    _WS_NAME_BY_ID.clear()


def is_configured() -> bool:
    c = get_fabric_connection()
    return bool(c.has_sp and c.workspace and c.models)


def mcp_launch_args() -> list:
    base = ["--start", "--skip-confirmation"]
    return base + (
        ["--authmode=serviceprincipal"] if get_fabric_connection().has_sp else []
    )


def _use_npx() -> bool:
    flag = os.getenv("MCP_USE_NPX", "").strip().lower()
    if flag in ("1", "true", "yes"):
        return True
    if flag in ("0", "false", "no"):
        return False
    return not (MCP_SERVER_PATH and os.path.isfile(MCP_SERVER_PATH))


def mcp_launch_command(args: list | None = None) -> tuple[str, list]:
    if args is None:
        args = mcp_launch_args()
    if _use_npx():
        npx = "npx.cmd" if os.name == "nt" else "npx"
        pkg = os.getenv("MCP_NPX_PACKAGE", "@microsoft/powerbi-modeling-mcp")
        return npx, ["-y", pkg, *args]
    return MCP_SERVER_PATH, list(args)
