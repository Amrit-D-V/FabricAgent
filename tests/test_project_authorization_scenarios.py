"""Scenario matrix for who is allowed to see which projects.

Authentication was never the hole: require_login() guards every page. The hole
was authorisation. db.database._list_all_projects is "SELECT * FROM projects"
with nothing in front of it, and the pages that built their project picker from
it offered a signed-in user every other client's work -- one click from moving
the whole app into a project they are not a member of, bound to a Fabric
workspace they cannot see.

list_projects_for_user is the only listing that answers for one person, and
auth.visible_projects is the only way a page reaches it. What is tested here is
the refusals: membership, the fail-closed Fabric gate, and a selection that
outlived the access it was made with.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from auth.workspace_access import FRESH, OFF, UNAVAILABLE, WorkspaceAccess

WS_A = "aaaaaaaa-1111-2222-3333-444444444444"
WS_B = "bbbbbbbb-5555-6666-7777-888888888888"

OWNER = "owner@client-a.example"
INTRUDER = "intruder@client-b.example"
ADMIN = "admin@fabricbi.example"


def _access(**ws):
    return {k.lower(): {"name": k, "is_read_only": v} for k, v in ws.items()}


def _gate(access, monkeypatch):
    from auth import workspace_access as WA

    monkeypatch.setattr(WA, "for_user", lambda e: access)


def _ids(projects):
    return sorted(p.id for p in projects)


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A real SQLite file holding two clients' projects.

    The membership rule is a SQL EXISTS clause, so a faked database would only
    prove that the fake had been written to agree with it.
    """
    from db import database

    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "projects.db"))
    monkeypatch.setattr(database, "_db_initialized_path", None)
    database.init_db()

    conn = database._get_connection()
    conn.execute("INSERT INTO clients (id, name) VALUES (1, 'Client')")
    conn.execute(
        "INSERT INTO projects (id, client_id, name, description, metadata) "
        "VALUES (1, 1, 'Client A rollout', '', '{}')"
    )
    conn.execute(
        "INSERT INTO projects (id, client_id, name, description, metadata) "
        "VALUES (2, 1, 'Client B rollout', '', '{}')"
    )
    conn.execute(
        "INSERT INTO project_members (project_id, user_email) VALUES (1, ?)",
        (OWNER,),
    )
    conn.commit()

    monkeypatch.setattr(database, "project_workspace_ids", lambda: {1: WS_A, 2: WS_B})
    yield database

    raw = getattr(database._thread_local, "conn", None)
    if raw is not None:
        raw.close()
        database._thread_local.conn = None


# --- Membership: the listing answers for one person -------------------------


def test_a_non_member_sees_none_of_another_users_projects(db, monkeypatch):
    """The whole point. Signing in to FabricBI is not admission to every client."""
    _gate(WorkspaceAccess({}, OFF), monkeypatch)
    assert db.list_projects_for_user(INTRUDER, False) == []


def test_a_member_sees_the_project_they_are_in_and_no_other(db, monkeypatch):
    _gate(WorkspaceAccess({}, OFF), monkeypatch)
    assert _ids(db.list_projects_for_user(OWNER, False)) == [1]


def test_membership_matches_regardless_of_address_case(db, monkeypatch):
    """Entra hands back whatever casing the user typed; the row is one string."""
    _gate(WorkspaceAccess({}, OFF), monkeypatch)
    assert _ids(db.list_projects_for_user(OWNER.upper(), False)) == [1]


@pytest.mark.parametrize("email", ["", "   ", "nobody@nowhere.example"])
def test_an_unknown_caller_is_a_member_of_nothing(db, monkeypatch, email):
    _gate(WorkspaceAccess({}, OFF), monkeypatch)
    assert db.list_projects_for_user(email, False) == []


# --- The Fabric gate, which admins do not escape ----------------------------


def test_an_admin_bypasses_membership(db, monkeypatch):
    """Stated so the next tests mean something: the admin path never looks at
    project_members, which leaves the Fabric gate as the only check on it."""
    _gate(WorkspaceAccess({}, OFF), monkeypatch)
    assert _ids(db.list_projects_for_user(ADMIN, True)) == [1, 2]


def test_an_admin_is_still_gated_on_the_fabric_workspace(db, monkeypatch):
    """The admin role governs FabricBI's own settings, not somebody else's Fabric
    data. An admin outside the workspace sees nothing bound to it."""
    _gate(WorkspaceAccess({}, FRESH), monkeypatch)
    assert db.list_projects_for_user(ADMIN, True) == []


def test_an_admin_sees_only_the_workspaces_they_are_in(db, monkeypatch):
    _gate(WorkspaceAccess(_access(**{WS_A: False}), FRESH), monkeypatch)
    assert _ids(db.list_projects_for_user(ADMIN, True)) == [1]


def test_the_gate_narrows_a_members_list_too(db, monkeypatch):
    """Membership and the workspace are two conditions, not alternatives."""
    _gate(WorkspaceAccess(_access(**{WS_B: False}), FRESH), monkeypatch)
    assert db.list_projects_for_user(OWNER, False) == []


@pytest.mark.parametrize("email,is_admin", [(ADMIN, True), (OWNER, False)])
def test_an_outage_empties_the_list_rather_than_filling_it(
    db, monkeypatch, email, is_admin
):
    """UNAVAILABLE means Power BI did not answer. Hiding projects through an
    outage is recoverable; opening every workspace to an admin is not."""
    _gate(WorkspaceAccess({}, UNAVAILABLE), monkeypatch)
    assert db.list_projects_for_user(email, is_admin) == []


def test_the_gate_off_leaves_the_membership_answer_alone(db, monkeypatch):
    """Before the delegated Power BI scope exists every lookup would fail, so
    OFF has to pass the list through rather than fail closed on all of it."""
    _gate(WorkspaceAccess({}, OFF), monkeypatch)
    assert _ids(db.list_projects_for_user(ADMIN, True)) == [1, 2]
    assert _ids(db.list_projects_for_user(OWNER, False)) == [1]


# --- What a page gets: auth.visible_projects --------------------------------


class _P:
    def __init__(self, pid, name=""):
        self.id = pid
        self.name = name or f"Project {pid}"


@pytest.fixture
def session():
    """The request scope auth.auth reads its identity from."""
    from core.context import begin, end, state

    token = begin()
    yield state()
    end(token)


@pytest.fixture
def listing(monkeypatch):
    """Record what visible_projects asks the cached listing for."""
    from auth import auth

    state = {"calls": [], "projects": [_P(1), _P(3)]}
    monkeypatch.setattr(
        auth,
        "_cached_projects_for_user",
        lambda email, is_admin: (
            state["calls"].append((email, is_admin)) or state["projects"]
        ),
    )
    return state


@pytest.mark.parametrize(
    "role,is_admin", [("viewer", False), ("editor", False), ("admin", True)]
)
def test_the_listing_is_asked_for_as_the_signed_in_user(
    session, listing, role, is_admin
):
    from auth.auth import visible_projects

    session["user"] = {"email": OWNER, "role": role}
    visible_projects()
    assert listing["calls"] == [(OWNER, is_admin)]


def test_no_signed_in_user_asks_for_nobody(session, listing):
    """A page reached without an identity must not fall back to an admin's."""
    from auth.auth import visible_projects

    visible_projects()
    assert listing["calls"] == [("", False)]


def test_a_stale_selection_is_not_in_the_list(session, listing):
    """Access is lost between reruns -- membership removed, workspace revoked --
    while active_project_id sits in session state saying otherwise."""
    from auth.auth import visible_projects

    session["user"] = {"email": OWNER, "role": "viewer"}
    session["active_project_id"] = 2
    session["active_project_name"] = "Client B rollout"

    assert _ids(visible_projects()) == [1, 3]


def test_a_stale_selection_is_dropped_rather_than_carried(session, listing):
    """Every page reads active_project_id right after listing. Left in place it
    would keep aiming pages at a project the list no longer contains."""
    from auth.auth import visible_projects

    session["user"] = {"email": OWNER, "role": "viewer"}
    session["active_project_id"] = 2
    session["active_project_name"] = "Client B rollout"

    visible_projects()
    assert session["active_project_id"] is None
    assert session["active_project_name"] == ""


def test_an_empty_list_drops_the_selection_too(session, listing):
    """The outage, seen from a page: nothing visible means nothing active."""
    from auth.auth import visible_projects

    session["user"] = {"email": OWNER, "role": "viewer"}
    session["active_project_id"] = 1
    listing["projects"] = []

    assert visible_projects() == []
    assert session["active_project_id"] is None


def test_a_selection_that_is_still_visible_is_left_alone(session, listing):
    """The guard must not throw people out of their own project every rerun."""
    from auth.auth import visible_projects

    session["user"] = {"email": OWNER, "role": "viewer"}
    session["active_project_id"] = 1
    session["active_project_name"] = "Project 1"

    visible_projects()
    assert session["active_project_id"] == 1
    assert session["active_project_name"] == "Project 1"


def test_no_selection_stays_no_selection(session, listing):
    from auth.auth import visible_projects

    session["user"] = {"email": OWNER, "role": "viewer"}

    visible_projects()
    assert session.get("active_project_id") is None


def test_every_caller_shares_one_cached_answer(session, monkeypatch):
    """Two answers to "which projects exist" would drift apart, and the picker
    would offer what a page refuses, or the reverse. One cache also means
    refresh_project_list() invalidates every caller at once."""
    from auth.auth import (
        _cached_projects_for_user,
        refresh_project_list,
        visible_projects,
    )
    from db import database

    calls = []
    monkeypatch.setattr(
        database,
        "list_projects_for_user",
        lambda email, is_admin: calls.append(email) or [_P(1)],
    )
    refresh_project_list()
    session["user"] = {"email": OWNER, "role": "viewer"}

    _cached_projects_for_user(OWNER, False)
    visible_projects()
    assert len(calls) == 1

    refresh_project_list()
    visible_projects()
    assert len(calls) == 2


# --- Regression guard -------------------------------------------------------

_UNAUTHORISED = "_list_all_projects"

_SKIP_DIRS = {
    ".git",
    ".venv",
    "FabricBI_beta",
    "__pycache__",
    "build_ctx",
    "lib",
    "node_modules",
    "venv",
}


def _source_files(root: Path):
    for entry in sorted(root.iterdir()):
        if entry.is_dir():
            if entry.name not in _SKIP_DIRS:
                yield from _source_files(entry)
        elif entry.suffix == ".py":
            yield entry


def _references_it(path: Path) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:
        return False  # cannot be imported, so it cannot be a caller
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == _UNAUTHORISED:
            return True
        if isinstance(node, ast.Attribute) and node.attr == _UNAUTHORISED:
            return True
        if isinstance(node, ast.ImportFrom) and any(
            alias.name == _UNAUTHORISED for alias in node.names
        ):
            return True
    return False


def test_nothing_outside_db_reaches_for_the_unauthorised_listing():
    """The fix is one import away from being undone by the next page that wants
    "the projects". Everything outside db/ goes through auth.visible_projects."""
    root = Path(__file__).resolve().parent.parent
    offenders = [
        str(path.relative_to(root))
        for path in _source_files(root)
        if path.relative_to(root).parts[0] != "db" and _references_it(path)
    ]
    assert offenders == []


def test_the_unauthorised_listing_keeps_its_private_name():
    """A public alias would put it back in reach of `from db.database import
    ...`, which is how it got everywhere in the first place."""
    from db import database

    assert hasattr(database, _UNAUTHORISED)
    assert not hasattr(database, "list_all_projects")
