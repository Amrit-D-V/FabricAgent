"""Scenario matrix for Fabric connection resolution.

get_fabric_connection resolves through four sources in order -- the active
project's binding, the library's default entry, the legacy single-row table,
then .env -- and caches the result. Each hop matters: land on the wrong one and
FabricBI talks to a different client's workspace with credentials that appear to
work. live_workspace() then sits on top, re-resolving the workspace NAME from
its stable ID so a rename in Fabric does not read as a permissions failure.

Every source is faked here. No DB, no REST, no tenant.
"""

from __future__ import annotations

import pytest

import fabric.fabric_connection as FC

ROW_TEMPLATE = {
    "tenant_id": "T",
    "client_id": "C",
    "client_secret": "S",
    "workspace_name": "DemoWorkspace-test",
    "workspace_id": "WS-1",
    "models": ["kaizen_v7"],
    "catalogs": {},
    "name": "Default entry",
}


def _row(**over):
    r = dict(ROW_TEMPLATE)
    r.update(over)
    return r


@pytest.fixture(autouse=True)
def reset_connection_module():
    """The resolver keeps module-level state. Without a reset, test order
    changes the answers -- which is exactly the bug class being tested."""
    FC.clear_fabric_connection_cache()
    FC.set_active_project(None)
    yield
    FC.clear_fabric_connection_cache()
    FC.set_active_project(None)


@pytest.fixture
def db(monkeypatch):
    """Stand in for db.database, which get_fabric_connection imports lazily."""
    from db import database

    state = {"resolve": None, "legacy": None, "resolve_calls": [], "raise": False}

    def resolve(pid):
        state["resolve_calls"].append(pid)
        if state["raise"]:
            raise RuntimeError("database is locked")
        return state["resolve"]

    monkeypatch.setattr(database, "resolve_fabric_connection", resolve)
    monkeypatch.setattr(database, "get_fabric_connection_row", lambda: state["legacy"])
    return state


# --- The four-source fallback chain -----------------------------------------


def test_project_binding_wins_when_present(db):
    db["resolve"] = _row(workspace_name="BoundWorkspace", name="ClientCo Fabric")
    conn = FC.get_fabric_connection()
    assert conn.workspace == "BoundWorkspace"
    assert conn.source == "db"
    assert conn.name == "ClientCo Fabric"


def test_legacy_row_is_used_only_when_the_library_is_empty(db):
    db["resolve"] = None
    db["legacy"] = _row(workspace_name="LegacyWorkspace")
    conn = FC.get_fabric_connection()
    assert conn.workspace == "LegacyWorkspace"
    assert conn.source == "db"


def test_env_is_the_last_resort(db):
    db["resolve"] = None
    db["legacy"] = None
    conn = FC.get_fabric_connection()
    assert conn.source == "env"


def test_a_broken_database_falls_through_to_env_instead_of_raising(db):
    """A locked or missing SQLite file must not take the whole app down -- the
    resolver logs and degrades to .env."""
    db["raise"] = True
    conn = FC.get_fabric_connection()
    assert conn.source == "env"


def test_models_and_catalogs_are_copied_not_aliased(db):
    """FabricConnection is frozen, but a shared mutable models list would let a
    caller mutate the DB row's list. models is a tuple by construction."""
    row = _row(models=["a", "b"], catalogs={"k": "v"})
    db["resolve"] = row
    conn = FC.get_fabric_connection()
    assert conn.models == ("a", "b")
    row["models"].append("c")
    row["catalogs"]["k2"] = "v2"
    assert conn.models == ("a", "b")
    assert conn.catalogs == {"k": "v"}


# --- Caching: the part that goes wrong when a project switches --------------


def test_the_active_connection_is_cached_after_the_first_read(db):
    db["resolve"] = _row()
    FC.get_fabric_connection()
    FC.get_fabric_connection()
    assert len(db["resolve_calls"]) == 1


def test_an_explicit_project_id_never_reads_or_writes_the_cache(db):
    """Asking about project 7 must not poison the active connection, or the
    next unqualified call answers for the wrong project."""
    db["resolve"] = _row(workspace_name="Active")
    active = FC.get_fabric_connection()
    db["resolve"] = _row(workspace_name="Other")
    other = FC.get_fabric_connection(project_id=7)
    assert other.workspace == "Other"
    assert db["resolve_calls"][-1] == 7
    assert FC.get_fabric_connection().workspace == active.workspace == "Active"


def test_switching_project_invalidates_the_cache(db):
    db["resolve"] = _row(workspace_name="First")
    assert FC.get_fabric_connection().workspace == "First"
    db["resolve"] = _row(workspace_name="Second")
    FC.set_active_project(2)
    assert FC.get_fabric_connection().workspace == "Second"


def test_setting_the_same_project_twice_does_not_invalidate(db):
    db["resolve"] = _row()
    FC.set_active_project(3)
    FC.get_fabric_connection()
    FC.set_active_project(3)
    FC.get_fabric_connection()
    assert len(db["resolve_calls"]) == 1


def test_the_active_project_id_is_what_gets_resolved(db):
    db["resolve"] = _row()
    FC.set_active_project(42)
    FC.get_fabric_connection()
    assert db["resolve_calls"] == [42]
    assert FC.get_active_project() == 42


# --- has_sp: all three or nothing -------------------------------------------


@pytest.mark.parametrize(
    "tenant,client,secret,expected",
    [
        ("T", "C", "S", True),
        ("", "C", "S", False),
        ("T", "", "S", False),
        ("T", "C", "", False),
        ("", "", "", False),
    ],
    ids=["complete", "no tenant", "no client", "no secret", "empty"],
)
def test_service_principal_requires_the_full_triple(tenant, client, secret, expected):
    conn = FC.FabricConnection(tenant, client, secret, "ws", "id")
    assert conn.has_sp is expected


def test_server_is_empty_without_a_workspace():
    """An empty workspace must not produce a half-formed XMLA address that
    fails deep inside ADOMD with an unrelated message."""
    assert FC.FabricConnection("T", "C", "S", "", "id").server == ""
    assert FC.FabricConnection("T", "C", "S", "ws", "id").server.endswith("/ws")


# --- live_workspace: the rename trap ----------------------------------------


@pytest.fixture
def rest(monkeypatch):
    from fabric import powerbi_rest

    state = {"workspaces": [], "calls": 0, "raise": False}

    def list_workspaces(token):
        state["calls"] += 1
        if state["raise"]:
            raise RuntimeError("403 Forbidden")
        return state["workspaces"]

    monkeypatch.setattr(powerbi_rest, "sp_powerbi_token", lambda *a: "tok")
    monkeypatch.setattr(powerbi_rest, "list_workspaces", list_workspaces)
    FC._WS_NAME_BY_ID.clear()
    yield state
    FC._WS_NAME_BY_ID.clear()


def test_a_renamed_workspace_resolves_to_its_current_name(rest):
    """The whole point: the saved row says 'DemoWorkspace', Fabric now says
    'DemoWorkspace-test', and XMLA addresses workspaces by name."""
    rest["workspaces"] = [("DemoWorkspace-test", "WS-1")]
    conn = FC.FabricConnection("T", "C", "S", "DemoWorkspace", "WS-1")
    assert conn.live_workspace() == "DemoWorkspace-test"


def test_an_unknown_id_falls_back_to_the_stored_name(rest):
    rest["workspaces"] = [("Something Else", "WS-9")]
    conn = FC.FabricConnection("T", "C", "S", "DemoWorkspace", "WS-1")
    assert conn.live_workspace() == "DemoWorkspace"


def test_a_rest_failure_falls_back_instead_of_propagating(rest):
    """A 403 on list_workspaces must not break a connection that would
    otherwise work under its stored name."""
    rest["raise"] = True
    conn = FC.FabricConnection("T", "C", "S", "DemoWorkspace", "WS-1")
    assert conn.live_workspace() == "DemoWorkspace"


def test_resolution_is_cached_across_connections(rest):
    rest["workspaces"] = [("DemoWorkspace-test", "WS-1")]
    conn = FC.FabricConnection("T", "C", "S", "DemoWorkspace", "WS-1")
    conn.live_workspace()
    conn.live_workspace()
    FC.FabricConnection("T", "C", "S", "DemoWorkspace", "WS-1").live_workspace()
    assert rest["calls"] == 1


def test_id_lookup_is_case_insensitive(rest):
    """Fabric returns GUIDs in mixed case depending on the endpoint; both
    casings are indexed on ingest."""
    rest["workspaces"] = [("DemoWorkspace-test", "ABC-DEF")]
    lower = FC.FabricConnection("T", "C", "S", "DemoWorkspace", "abc-def")
    assert lower.live_workspace() == "DemoWorkspace-test"


@pytest.mark.parametrize(
    "workspace_id,tenant,client,secret",
    [
        ("", "T", "C", "S"),
        ("WS-1", "", "C", "S"),
        ("WS-1", "T", "C", ""),
    ],
    ids=["no workspace id", "no tenant", "no secret"],
)
def test_no_id_or_no_service_principal_skips_rest_entirely(
    rest, workspace_id, tenant, client, secret
):
    conn = FC.FabricConnection(tenant, client, secret, "Stored", workspace_id)
    assert conn.live_workspace() == "Stored"
    assert rest["calls"] == 0


def test_clearing_the_cache_also_clears_resolved_names(rest, db):
    rest["workspaces"] = [("DemoWorkspace-test", "WS-1")]
    conn = FC.FabricConnection("T", "C", "S", "DemoWorkspace", "WS-1")
    assert conn.live_workspace() == "DemoWorkspace-test"
    FC.clear_fabric_connection_cache()
    rest["workspaces"] = [("DemoWorkspace-renamed-again", "WS-1")]
    assert conn.live_workspace() == "DemoWorkspace-renamed-again"


# --- is_configured: all three conditions ------------------------------------


@pytest.mark.parametrize(
    "row,expected",
    [
        ({"models": ["m"]}, True),
        ({"models": []}, False),
        ({"models": ["m"], "workspace_name": ""}, False),
        ({"models": ["m"], "client_secret": ""}, False),
    ],
    ids=["complete", "no models", "no workspace", "no secret"],
)
def test_is_configured_requires_sp_workspace_and_models(db, row, expected):
    db["resolve"] = _row(**row)
    assert FC.is_configured() is expected


# --- MCP launch: SP flag and npx-vs-local selection -------------------------


def test_service_principal_adds_the_authmode_flag(db):
    db["resolve"] = _row()
    assert "--authmode=serviceprincipal" in FC.mcp_launch_args()


def test_no_service_principal_omits_the_authmode_flag(db):
    db["resolve"] = _row(client_secret="")
    assert "--authmode=serviceprincipal" not in FC.mcp_launch_args()
    assert FC.mcp_launch_args()[:2] == ["--start", "--skip-confirmation"]


@pytest.mark.parametrize("flag", ["1", "true", "TRUE", "yes", "Yes"])
def test_npx_is_forced_on(monkeypatch, flag):
    monkeypatch.setenv("MCP_USE_NPX", flag)
    monkeypatch.setattr(FC, "MCP_SERVER_PATH", __file__)
    assert FC._use_npx() is True


@pytest.mark.parametrize("flag", ["0", "false", "FALSE", "no", "No"])
def test_npx_is_forced_off_even_without_a_local_binary(monkeypatch, flag):
    monkeypatch.setenv("MCP_USE_NPX", flag)
    monkeypatch.setattr(FC, "MCP_SERVER_PATH", "")
    assert FC._use_npx() is False


def test_unset_flag_falls_back_to_whether_the_binary_exists(monkeypatch):
    monkeypatch.delenv("MCP_USE_NPX", raising=False)
    monkeypatch.setattr(FC, "MCP_SERVER_PATH", __file__)
    assert FC._use_npx() is False
    monkeypatch.setattr(FC, "MCP_SERVER_PATH", "/nope/does/not/exist")
    assert FC._use_npx() is True


@pytest.mark.parametrize("flag", ["maybe", "", "  ", "2"])
def test_an_unrecognised_flag_is_treated_as_unset(monkeypatch, flag):
    monkeypatch.setenv("MCP_USE_NPX", flag)
    monkeypatch.setattr(FC, "MCP_SERVER_PATH", __file__)
    assert FC._use_npx() is False


def test_npx_command_carries_the_package_and_args(monkeypatch):
    monkeypatch.setenv("MCP_USE_NPX", "1")
    monkeypatch.setenv("MCP_NPX_PACKAGE", "@microsoft/powerbi-modeling-mcp")
    cmd, args = FC.mcp_launch_command(["--start"])
    assert cmd.startswith("npx")
    assert args == ["-y", "@microsoft/powerbi-modeling-mcp", "--start"]


def test_local_command_is_the_configured_path(monkeypatch):
    monkeypatch.setenv("MCP_USE_NPX", "0")
    monkeypatch.setattr(FC, "MCP_SERVER_PATH", r"C:\tools\mcp.exe")
    cmd, args = FC.mcp_launch_command(["--start"])
    assert cmd == r"C:\tools\mcp.exe"
    assert args == ["--start"]
