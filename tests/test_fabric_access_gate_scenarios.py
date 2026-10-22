"""Scenario matrix for Fabric-level access control.

FabricBI reaches Fabric as one shared service principal, so without this gate every
authorised FabricBI user inherits the SP's permissions on every workspace. The gate
makes Power BI the authority: a project is reachable only if the signed-in user
can see the workspace its connection is bound to, asked against THEIR identity.

The interesting cases are the failures, not the happy path. An outage must hide
projects rather than expose them; a stale cache must not outlive someone's
removal; and an unknown answer must never be read as "allowed".
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import config
from auth import workspace_access as WA
from auth.workspace_access import FRESH, OFF, STALE, UNAVAILABLE, WorkspaceAccess

WS_A = "aaaaaaaa-1111-2222-3333-444444444444"
WS_B = "bbbbbbbb-5555-6666-7777-888888888888"


def _access(**ws):
    return {k.lower(): {"name": k, "is_read_only": v} for k, v in ws.items()}


# --- The decision object ----------------------------------------------------


@pytest.mark.parametrize(
    "status,usable",
    [(FRESH, True), (STALE, True), (OFF, True), (UNAVAILABLE, False)],
)
def test_only_a_known_answer_may_grant(status, usable):
    """UNAVAILABLE is not 'no workspaces' -- it is 'we do not know', and an
    unknown answer must never open a door."""
    assert WorkspaceAccess({}, status).usable is usable


def test_a_visible_workspace_is_visible():
    a = WorkspaceAccess(_access(**{WS_A: False}), FRESH)
    assert a.can_see(WS_A) is True


def test_a_workspace_the_user_cannot_see_is_refused():
    a = WorkspaceAccess(_access(**{WS_A: False}), FRESH)
    assert a.can_see(WS_B) is False


def test_an_outage_refuses_everything_it_previously_allowed():
    """Fail closed. Hiding a project during a Power BI outage is recoverable;
    showing another client's workspace is not."""
    a = WorkspaceAccess(_access(**{WS_A: False}), UNAVAILABLE)
    assert a.can_see(WS_A) is False


def test_workspace_ids_match_regardless_of_case():
    """Power BI returns GUIDs in mixed case depending on the endpoint."""
    a = WorkspaceAccess(_access(**{WS_A: False}), FRESH)
    assert a.can_see(WS_A.upper()) is True


@pytest.mark.parametrize("wsid", ["", "   ", None])
def test_a_blank_workspace_id_is_not_a_match(wsid):
    a = WorkspaceAccess(_access(**{WS_A: False}), FRESH)
    assert a.can_see(wsid) is False


def test_gate_disabled_allows_everything():
    """Before the split app registration exists there is no delegated scope, so
    every lookup would fail and a fail-closed gate would hide every project."""
    a = WorkspaceAccess({}, OFF)
    assert a.can_see(WS_A) is True
    assert a.is_read_only(WS_A) is False


# --- Read-only capping ------------------------------------------------------


def test_a_read_only_workspace_reports_read_only():
    a = WorkspaceAccess(_access(**{WS_A: True}), FRESH)
    assert a.is_read_only(WS_A) is True


def test_a_writable_workspace_reports_writable():
    a = WorkspaceAccess(_access(**{WS_A: False}), FRESH)
    assert a.is_read_only(WS_A) is False


def test_an_unknown_workspace_is_treated_as_read_only():
    """If we cannot prove write access, we do not grant it."""
    a = WorkspaceAccess(_access(**{WS_A: False}), FRESH)
    assert a.is_read_only(WS_B) is True


def test_an_outage_caps_everything_to_read_only():
    a = WorkspaceAccess(_access(**{WS_A: False}), UNAVAILABLE)
    assert a.is_read_only(WS_A) is True


# --- Cache freshness --------------------------------------------------------


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setattr(config, "FABRIC_ACCESS_GATE", True)
    monkeypatch.setattr(WA, "FABRIC_ACCESS_GATE", True)
    monkeypatch.setattr(WA, "FABRIC_ACCESS_TTL_S", 900)
    monkeypatch.setattr(WA, "FABRIC_ACCESS_ALLOW_STALE", False)


@pytest.fixture
def store(monkeypatch):
    """Stand in for the user_workspace_access table."""
    state = {"rows": {}, "saved": [], "refreshes": 0}

    def get_access(email):
        return state["rows"]

    def save_access(email, workspaces):
        state["saved"].append((email, workspaces))

    from db import database

    monkeypatch.setattr(database, "get_user_workspace_access", get_access)
    monkeypatch.setattr(database, "save_user_workspace_access", save_access)
    return state


def _row(name, read_only, age_seconds):
    return {
        "name": name,
        "is_read_only": read_only,
        "checked_at": (datetime.now() - timedelta(seconds=age_seconds)).isoformat(),
    }


def test_a_fresh_cache_is_served_without_calling_power_bi(gate_on, store, monkeypatch):
    store["rows"] = {WS_A: _row("Client A", False, 60)}
    monkeypatch.setattr(
        WA, "refresh", lambda e: pytest.fail("should not refresh a fresh cache")
    )
    result = WA.for_user("a@b.c")
    assert result.status == FRESH
    assert result.can_see(WS_A)


def test_an_expired_cache_triggers_a_refresh(gate_on, store, monkeypatch):
    """Removing someone in Fabric has to take effect, so the cache must expire."""
    store["rows"] = {WS_A: _row("Client A", False, 5000)}
    monkeypatch.setattr(
        WA, "refresh", lambda e: WorkspaceAccess(_access(**{WS_B: False}), FRESH)
    )
    result = WA.for_user("a@b.c")
    assert result.can_see(WS_B) and not result.can_see(WS_A)


def test_force_bypasses_a_fresh_cache(gate_on, store, monkeypatch):
    store["rows"] = {WS_A: _row("Client A", False, 10)}
    monkeypatch.setattr(
        WA, "refresh", lambda e: WorkspaceAccess(_access(**{WS_B: False}), FRESH)
    )
    assert WA.for_user("a@b.c", force=True).can_see(WS_B)


def test_a_failed_refresh_with_no_stale_allowance_fails_closed(
    gate_on, store, monkeypatch
):
    store["rows"] = {WS_A: _row("Client A", False, 5000)}
    monkeypatch.setattr(
        WA, "refresh", lambda e: WorkspaceAccess({}, UNAVAILABLE, "Power BI down")
    )
    result = WA.for_user("a@b.c")
    assert result.status == UNAVAILABLE
    assert not result.usable
    assert not result.can_see(WS_A)


def test_stale_is_served_only_when_explicitly_allowed(gate_on, store, monkeypatch):
    monkeypatch.setattr(WA, "FABRIC_ACCESS_ALLOW_STALE", True)
    store["rows"] = {WS_A: _row("Client A", False, 5000)}
    monkeypatch.setattr(
        WA, "refresh", lambda e: WorkspaceAccess({}, UNAVAILABLE, "Power BI down")
    )
    result = WA.for_user("a@b.c")
    assert result.status == STALE
    assert result.can_see(WS_A)
    assert "unreachable" in result.reason


def test_an_empty_cache_never_counts_as_fresh(gate_on, store, monkeypatch):
    """No rows means never checked, not 'checked and found nothing'."""
    store["rows"] = {}
    calls = []
    monkeypatch.setattr(
        WA, "refresh", lambda e: calls.append(e) or WorkspaceAccess({}, FRESH)
    )
    WA.for_user("a@b.c")
    assert calls == ["a@b.c"]


def test_no_signed_in_user_is_unavailable_not_permissive(gate_on, store):
    result = WA.for_user("")
    assert result.status == UNAVAILABLE
    assert not result.can_see(WS_A)


def test_the_gate_off_short_circuits_before_any_lookup(monkeypatch, store):
    monkeypatch.setattr(WA, "FABRIC_ACCESS_GATE", False)
    monkeypatch.setattr(
        WA, "refresh", lambda e: pytest.fail("must not call Power BI when off")
    )
    assert WA.for_user("a@b.c").status == OFF


# --- Refresh: what Power BI actually returns --------------------------------


def test_refresh_maps_the_power_bi_payload(gate_on, store, monkeypatch):
    from auth import entra_sso

    monkeypatch.setattr(entra_sso, "acquire_powerbi_token", lambda e: ("tok", ""))
    monkeypatch.setattr(
        WA,
        "_fetch_workspaces",
        lambda t: [
            {"id": WS_A, "name": "Client A", "isReadOnly": False},
            {"id": WS_B, "name": "Client B", "isReadOnly": True},
        ],
    )
    result = WA.refresh("a@b.c")
    assert result.status == FRESH
    assert result.can_see(WS_A) and result.can_see(WS_B)
    assert result.is_read_only(WS_B) and not result.is_read_only(WS_A)


def test_refresh_replaces_rather_than_merges(gate_on, store, monkeypatch):
    """A workspace the user has lost access to must vanish. A merge would keep
    it forever, which is the whole failure this gate exists to prevent."""
    from auth import entra_sso

    store["rows"] = {WS_A: _row("Client A", False, 10)}
    monkeypatch.setattr(entra_sso, "acquire_powerbi_token", lambda e: ("tok", ""))
    monkeypatch.setattr(
        WA,
        "_fetch_workspaces",
        lambda t: [{"id": WS_B, "name": "B", "isReadOnly": False}],
    )
    result = WA.refresh("a@b.c")
    assert not result.can_see(WS_A)
    _saved_email, saved_ws = store["saved"][-1]
    assert [w["id"] for w in saved_ws] == [WS_B]


def test_a_user_with_no_workspaces_is_a_valid_fresh_answer(gate_on, store, monkeypatch):
    """Distinct from an outage: we asked, and the answer was none."""
    from auth import entra_sso

    monkeypatch.setattr(entra_sso, "acquire_powerbi_token", lambda e: ("tok", ""))
    monkeypatch.setattr(WA, "_fetch_workspaces", lambda t: [])
    result = WA.refresh("a@b.c")
    assert result.status == FRESH and result.usable
    assert not result.can_see(WS_A)


def test_no_token_is_unavailable_not_empty(gate_on, store, monkeypatch):
    from auth import entra_sso

    monkeypatch.setattr(
        entra_sso, "acquire_powerbi_token", lambda e: ("", "consent required")
    )
    result = WA.refresh("a@b.c")
    assert result.status == UNAVAILABLE
    assert "consent" in result.reason


def test_a_power_bi_error_is_unavailable_not_empty(gate_on, store, monkeypatch):
    from auth import entra_sso

    def boom(t):
        raise RuntimeError("503 Service Unavailable")

    monkeypatch.setattr(entra_sso, "acquire_powerbi_token", lambda e: ("tok", ""))
    monkeypatch.setattr(WA, "_fetch_workspaces", boom)
    result = WA.refresh("a@b.c")
    assert result.status == UNAVAILABLE
    assert "503" in result.reason


def test_a_caching_failure_does_not_deny_access(gate_on, store, monkeypatch):
    """The cache is an optimisation. If the write fails the answer still stands."""
    from auth import entra_sso
    from db import database

    def bad_save(email, ws):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(database, "save_user_workspace_access", bad_save)
    monkeypatch.setattr(entra_sso, "acquire_powerbi_token", lambda e: ("tok", ""))
    monkeypatch.setattr(
        WA,
        "_fetch_workspaces",
        lambda t: [{"id": WS_A, "name": "A", "isReadOnly": False}],
    )
    result = WA.refresh("a@b.c")
    assert result.status == FRESH and result.can_see(WS_A)


# --- Project filtering ------------------------------------------------------


class _P:
    def __init__(self, pid):
        self.id = pid


@pytest.fixture
def projects(monkeypatch):
    from db import database

    monkeypatch.setattr(database, "project_workspace_ids", lambda: {1: WS_A, 2: WS_B})
    return database


def test_only_projects_in_a_visible_workspace_survive(projects, monkeypatch):
    monkeypatch.setattr(
        WA, "for_user", lambda e: WorkspaceAccess(_access(**{WS_A: False}), FRESH)
    )
    kept = projects._apply_fabric_gate([_P(1), _P(2)], "a@b.c")
    assert [p.id for p in kept] == [1]


def test_an_unbound_project_is_left_alone(projects, monkeypatch):
    """No connection means no workspace to check it against; project_members
    still applies."""
    monkeypatch.setattr(
        WA, "for_user", lambda e: WorkspaceAccess(_access(**{WS_A: False}), FRESH)
    )
    kept = projects._apply_fabric_gate([_P(1), _P(2), _P(3)], "a@b.c")
    assert [p.id for p in kept] == [1, 3]


def test_an_outage_hides_every_project(projects, monkeypatch):
    monkeypatch.setattr(WA, "for_user", lambda e: WorkspaceAccess({}, UNAVAILABLE))
    assert projects._apply_fabric_gate([_P(1), _P(2), _P(3)], "a@b.c") == []


def test_the_gate_off_changes_nothing(projects, monkeypatch):
    monkeypatch.setattr(WA, "for_user", lambda e: WorkspaceAccess({}, OFF))
    kept = projects._apply_fabric_gate([_P(1), _P(2)], "a@b.c")
    assert [p.id for p in kept] == [1, 2]


def test_admins_are_gated_too(projects, monkeypatch):
    """A FabricBI admin who is not in the workspace has no business opening a
    project bound to it. The admin role governs FabricBI's settings, not somebody
    else's Fabric data."""
    monkeypatch.setattr(WA, "for_user", lambda e: WorkspaceAccess({}, FRESH))
    assert projects._apply_fabric_gate([_P(1), _P(2)], "admin@b.c") == []
