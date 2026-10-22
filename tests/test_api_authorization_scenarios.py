"""Scenario matrix for what the HTTP API lets through.

Under Streamlit, "is this person allowed" was answered by each page calling
require_login()/require_capability() near the top of its script. Missing the
call on one page was invisible -- the page simply worked for everyone.

The API answers it in one place instead (api.registry.route), so these pin the
decorator rather than the endpoints: an unauthenticated caller gets 401, a
caller below the required role gets 403, and a project id that is not in the
caller's own listing is refused even when it exists.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from auth.session_cookie import COOKIE_NAME, serialize_session

ADMIN = {"email": "admin@fabricbi.example", "role": "admin", "is_active": 1}
EDITOR = {"email": "editor@fabricbi.example", "role": "editor", "is_active": 1}
VIEWER = {"email": "viewer@fabricbi.example", "role": "viewer", "is_active": 1}


class _P:
    def __init__(self, pid, name="Project"):
        self.id = pid
        self.name = name
        self.description = ""
        self.client_id = 1
        self.status = "active"


@pytest.fixture
def users(monkeypatch):
    """app_users, as the API re-reads it on every request."""
    table = {u["email"]: dict(u) for u in (ADMIN, EDITOR, VIEWER)}
    # auth.auth binds get_app_user at import time and the last-admin guard
    # imports it from db.database at call time. Same function in production,
    # so both bindings point at the one fake table here.
    monkeypatch.setattr("auth.auth.get_app_user", lambda e: table.get(e))
    monkeypatch.setattr("db.database.get_app_user", lambda e: table.get(e))
    return table


@pytest.fixture
def projects(monkeypatch):
    """Two projects, both visible to whoever asks."""
    listing = [_P(1, "Alpha"), _P(2, "Beta")]
    monkeypatch.setattr(
        "auth.auth._cached_projects_for_user", lambda email, is_admin: listing
    )
    return listing


@pytest.fixture
def client(users, projects, monkeypatch):
    # effective_role() consults Fabric; with no workspace bound it is a no-op,
    # but pin it so a real network call can never leak into these.
    monkeypatch.setattr("auth.auth.active_workspace_id", lambda: "")
    from api.server import app

    return TestClient(app)


def _as(client: TestClient, user: dict) -> TestClient:
    client.cookies.set(
        COOKIE_NAME, serialize_session(user["email"], user["role"], user["email"])
    )
    return client


# --- Signed out --------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/projects"),
        ("POST", "/projects"),
        ("GET", "/users"),
        ("GET", "/audit"),
        ("POST", "/chat"),
        ("GET", "/me/capabilities"),
    ],
)
def test_an_unauthenticated_caller_is_refused(client, method, path):
    assert client.request(method, path).status_code == 401


def test_the_session_endpoint_is_reachable_signed_out(client):
    """The shell asks who you are before it can know to show a sign-in page."""
    body = client.get("/session").json()
    assert body["user"] is None


def test_health_is_reachable_signed_out(client):
    assert client.get("/health").status_code == 200


# --- A cookie is not a role --------------------------------------------------


def test_a_forged_cookie_is_refused(client):
    """The cookie is encrypted; arbitrary bytes must not become a session."""
    client.cookies.set(COOKIE_NAME, "not-a-real-token")
    assert client.get("/projects").status_code == 401


def test_a_cookie_for_a_deleted_account_is_refused(client, users):
    _as(client, ADMIN)
    users.pop(ADMIN["email"])
    assert client.get("/projects").status_code == 401


def test_a_cookie_for_a_disabled_account_is_refused(client, users):
    _as(client, ADMIN)
    users[ADMIN["email"]]["is_active"] = 0
    assert client.get("/projects").status_code == 401


def test_the_role_is_re_read_not_taken_from_the_cookie(client, users):
    """A cookie minted while the user was admin must not outlive the demotion."""
    _as(client, ADMIN)
    users[ADMIN["email"]]["role"] = "viewer"
    assert client.get("/users").status_code == 403


# --- Role gates --------------------------------------------------------------


def test_a_viewer_may_read_projects(client):
    _as(client, VIEWER)
    assert client.get("/projects").status_code == 200


def test_a_viewer_may_not_create_a_project(client):
    _as(client, VIEWER)
    assert client.post("/projects", json={"name": "New"}).status_code == 403


def test_an_editor_may_not_reach_user_administration(client):
    _as(client, EDITOR)
    assert client.get("/users").status_code == 403


def test_an_editor_may_not_read_the_audit_log(client):
    _as(client, EDITOR)
    assert client.get("/audit").status_code == 403


def test_an_admin_may_reach_user_administration(client, monkeypatch):
    monkeypatch.setattr("db.database.list_app_users", lambda: [])
    _as(client, ADMIN)
    assert client.get("/users").status_code == 200


def test_an_editor_may_not_delete_a_project(client):
    """Deleting is admin-only even for the role that can create."""
    _as(client, EDITOR)
    assert client.delete("/projects/1").status_code == 403


# --- Project scoping ---------------------------------------------------------


def test_a_project_outside_the_listing_is_refused(client, monkeypatch):
    """The id comes from the client, so membership is re-checked server-side."""
    _as(client, ADMIN)
    assert client.get("/projects/99").status_code == 403


def test_a_project_inside_the_listing_is_allowed(client, monkeypatch):
    monkeypatch.setattr("db.database.get_project", lambda pid: _P(1, "Alpha"))
    _as(client, ADMIN)
    response = client.get("/projects/1")
    assert response.status_code == 200
    assert response.json()["name"] == "Alpha"


def test_the_listing_only_returns_what_the_user_may_see(client, projects):
    _as(client, VIEWER)
    assert [p["id"] for p in client.get("/projects").json()] == [1, 2]


# --- The active project rides on a header ------------------------------------


def test_the_active_project_header_reaches_the_request_scope(client):
    _as(client, ADMIN)
    body = client.get("/me/capabilities", headers={"X-FabricBI-Project": "1"}).json()
    assert body["role"] == "admin"


def test_a_junk_project_header_is_ignored(client):
    """A bad header must not 500 the request."""
    _as(client, ADMIN)
    assert (
        client.get("/me/capabilities", headers={"X-FabricBI-Project": "abc"}).status_code
        == 200
    )


# --- Last-admin guard --------------------------------------------------------


def test_the_last_admin_cannot_be_demoted(client, monkeypatch):
    monkeypatch.setattr("db.database.count_active_admins", lambda: 1)
    _as(client, ADMIN)
    response = client.patch(f"/users/{ADMIN['email']}", json={"role": "viewer"})
    assert response.status_code == 403
    assert "last active admin" in response.json()["error"]


def test_the_last_admin_cannot_be_disabled(client, monkeypatch):
    monkeypatch.setattr("db.database.count_active_admins", lambda: 1)
    _as(client, ADMIN)
    assert (
        client.patch(f"/users/{ADMIN['email']}", json={"active": False}).status_code
        == 403
    )


def test_demotion_is_allowed_when_another_admin_remains(client, monkeypatch):
    monkeypatch.setattr("db.database.count_active_admins", lambda: 2)
    monkeypatch.setattr("db.database.update_user_role", lambda e, r: None)
    _as(client, ADMIN)
    assert (
        client.patch(f"/users/{ADMIN['email']}", json={"role": "viewer"}).status_code
        == 200
    )


def test_you_cannot_delete_your_own_account(client, monkeypatch):
    monkeypatch.setattr("db.database.count_active_admins", lambda: 5)
    _as(client, ADMIN)
    response = client.delete(f"/users/{ADMIN['email']}")
    assert response.status_code == 403


# --- Signing out -------------------------------------------------------------


def test_signing_out_clears_the_cookie(client, monkeypatch):
    monkeypatch.setattr("auth.auth.audit", lambda *a, **k: None)
    monkeypatch.setattr("auth.entra_sso.sign_out", lambda e: None)
    _as(client, ADMIN)
    response = client.post("/logout")
    assert response.status_code == 200
    # A real Set-Cookie deletion, which the Streamlit iframe could never do.
    assert f'{COOKIE_NAME}=""' in response.headers.get("set-cookie", "") or (
        f"{COOKIE_NAME}=;" in response.headers.get("set-cookie", "")
    )


def test_a_bad_body_is_a_400_not_a_500(client):
    _as(client, ADMIN)
    response = client.post(
        "/chat", content="not json", headers={"content-type": "application/json"}
    )
    assert response.status_code == 400


@pytest.fixture
def resource_db(monkeypatch):
    import sqlite3
    from db import database

    raw = sqlite3.connect(":memory:", check_same_thread=False)
    raw.row_factory = sqlite3.Row
    raw.executescript("""
        CREATE TABLE project_rules (id INTEGER PRIMARY KEY, project_id INTEGER, is_active INTEGER);
        INSERT INTO project_rules VALUES (7, 1, 1), (8, 2, 1);
        CREATE TABLE documents (id INTEGER PRIMARY KEY, project_id INTEGER, filename TEXT, original_name TEXT, file_type TEXT, file_size INTEGER, uploaded_at TEXT, file_path TEXT, description TEXT);
        INSERT INTO documents VALUES (7, 1, 'f1.txt', 'f1.txt', 'text/plain', 10, '', 'f1.txt', ''), (8, 2, 'f2.txt', 'f2.txt', 'text/plain', 10, '', 'f2.txt', '');
    """)
    monkeypatch.setattr(database, "_get_connection", lambda: database._SqliteConn(raw))
    yield raw
    raw.close()


@pytest.mark.parametrize("resource_id,status", [(7, 200), (8, 404), (999, 404)])
def test_rule_delete_checks_resource_project(client, projects, resource_db, resource_id, status):
    projects[:] = [_P(1)]
    response = _as(client, EDITOR).delete(
        f"/builder/rules/{resource_id}", headers={"X-FabricBI-Project": "1"}
    )
    assert response.status_code == status
    rows = resource_db.execute("SELECT id, is_active FROM project_rules ORDER BY id").fetchall()
    assert [(r[0], r[1]) for r in rows] == [(7, 0 if resource_id == 7 else 1), (8, 1)]


@pytest.mark.parametrize("resource_id,status", [(7, 200), (8, 404), (999, 404)])
def test_document_delete_checks_resource_project(
    client, projects, resource_db, resource_id, status
):
    projects[:] = [_P(1)]
    response = _as(client, EDITOR).delete(
        f"/documents/{resource_id}", headers={"X-FabricBI-Project": "1"}
    )
    assert response.status_code == status
    remaining = [r[0] for r in resource_db.execute("SELECT id FROM documents ORDER BY id").fetchall()]
    assert remaining == ([8] if resource_id == 7 else [7, 8])
