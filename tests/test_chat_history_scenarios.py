"""Scenario matrix for the assistant's saved transcript.

Chat history is keyed on (project, user). Two things follow from that and both
are easy to get wrong:

  * Switching project must swap the thread, not carry it over -- a transcript
    about one client's model showing up under another is a data leak, not a
    cosmetic bug.
  * Clearing must delete only your own thread, not every member's.

A conversation with no project selected has nothing to key on, so it is not
saved at all -- and the UI has to say so rather than pretending it was.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from auth.session_cookie import COOKIE_NAME, serialize_session

OWNER = {"email": "owner@fabricbi.example", "role": "admin", "is_active": 1}
OTHER = {"email": "other@fabricbi.example", "role": "editor", "is_active": 1}


class _P:
    def __init__(self, pid, name="Project"):
        self.id = pid
        self.name = name


@pytest.fixture
def store(monkeypatch):
    """An in-memory stand-in for chat_history_v2, keyed the way the table is."""
    saved: dict[tuple[int, str], list] = {}

    def save(project_id, messages, _claude, user_email=""):
        saved[(project_id, user_email.lower())] = list(messages)

    def load(project_id, user_email=""):
        return list(saved.get((project_id, user_email.lower()), [])), []

    def clear(project_id, user_email=None):
        if user_email is None:
            for key in [k for k in saved if k[0] == project_id]:
                saved.pop(key)
        else:
            saved.pop((project_id, user_email.lower()), None)

    monkeypatch.setattr("db.database.save_chat_state", save)
    monkeypatch.setattr("db.database.load_chat_state", load)
    monkeypatch.setattr("db.database.clear_chat_state", clear)
    return saved


@pytest.fixture
def client(monkeypatch, store):
    table = {u["email"]: dict(u) for u in (OWNER, OTHER)}
    monkeypatch.setattr("auth.auth.get_app_user", lambda e: table.get(e))
    monkeypatch.setattr("db.database.get_app_user", lambda e: table.get(e))
    monkeypatch.setattr(
        "auth.auth._cached_projects_for_user", lambda email, is_admin: [_P(1), _P(2)]
    )
    monkeypatch.setattr("auth.auth.active_workspace_id", lambda: "")
    monkeypatch.setattr(
        "api.routes_chat.run_chat",
        lambda messages, mode="dashboard": {
            "steps": ["connected"],
            "content": "an answer",
            "model": "test",
            "usage": {},
        },
    )
    from api.server import app

    return TestClient(app)


def _as(client, user):
    client.cookies.set(COOKIE_NAME, serialize_session(user["email"], user["role"], ""))
    return client


def _project(pid):
    return {"X-FabricBI-Project": str(pid)}


# --- A turn is saved --------------------------------------------------------


def test_a_turn_is_saved_against_the_project(client, store):
    _as(client, OWNER)
    client.post("/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers=_project(1))
    assert (1, OWNER["email"]) in store


def test_the_saved_thread_holds_both_sides(client, store):
    _as(client, OWNER)
    client.post("/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers=_project(1))
    thread = store[(1, OWNER["email"])]
    assert [m["role"] for m in thread] == ["user", "assistant"]
    assert thread[-1]["content"] == "an answer"


def test_the_tool_trail_is_saved_too(client, store):
    """The steps are what let a reloaded thread still show what the assistant
    actually did, rather than just its final answer."""
    _as(client, OWNER)
    client.post("/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers=_project(1))
    assert store[(1, OWNER["email"])][-1]["steps"] == ["connected"]


def test_history_comes_back(client):
    _as(client, OWNER)
    client.post("/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers=_project(1))
    body = client.get("/chat/history", headers=_project(1)).json()
    assert body["persisted"] is True
    assert body["messages"][0]["content"] == "hi"


# --- Scoped to the project --------------------------------------------------


def test_another_project_has_its_own_thread(client):
    """A transcript about one client must not appear under another."""
    _as(client, OWNER)
    client.post("/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers=_project(1))
    assert client.get("/chat/history", headers=_project(2)).json()["messages"] == []


def test_no_project_means_nothing_is_saved(client, store):
    _as(client, OWNER)
    client.post("/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert store == {}


def test_no_project_reports_that_it_is_not_persisted(client):
    """The UI says "not saved" on the strength of this flag, so it must be
    honest rather than defaulting to True."""
    _as(client, OWNER)
    assert client.get("/chat/history").json()["persisted"] is False


def test_a_project_the_caller_cannot_see_is_refused(client):
    _as(client, OWNER)
    assert client.get("/chat/history", headers=_project(99)).status_code == 403


# --- Scoped to the user -----------------------------------------------------


def test_two_users_on_one_project_keep_separate_threads(client):
    _as(client, OWNER)
    client.post("/chat", json={"messages": [{"role": "user", "content": "owner"}]}, headers=_project(1))

    _as(client, OTHER)
    assert client.get("/chat/history", headers=_project(1)).json()["messages"] == []


def test_clearing_removes_only_your_own_thread(client, store):
    _as(client, OWNER)
    client.post("/chat", json={"messages": [{"role": "user", "content": "a"}]}, headers=_project(1))
    _as(client, OTHER)
    client.post("/chat", json={"messages": [{"role": "user", "content": "b"}]}, headers=_project(1))

    client.delete("/chat/history", headers=_project(1))  # as OTHER
    assert (1, OWNER["email"]) in store
    assert (1, OTHER["email"]) not in store


def test_clearing_with_no_project_is_a_no_op(client, store):
    _as(client, OWNER)
    client.post("/chat", json={"messages": [{"role": "user", "content": "a"}]}, headers=_project(1))
    client.delete("/chat/history")
    assert (1, OWNER["email"]) in store


# --- Failures must not corrupt the thread -----------------------------------


def test_a_failed_turn_is_not_saved(client, store, monkeypatch):
    """Otherwise the transcript keeps a question with no answer after it."""
    def boom(messages, mode="dashboard"):
        raise RuntimeError("provider down")

    monkeypatch.setattr("api.routes_chat.run_chat", boom)
    _as(client, OWNER)
    client.post("/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers=_project(1))
    assert (1, OWNER["email"]) not in store


def test_a_failing_save_does_not_lose_the_answer(client, monkeypatch):
    """The reply is what the user is waiting for; a storage problem must not
    turn a good answer into an error."""
    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr("db.database.save_chat_state", boom)
    _as(client, OWNER)
    response = client.post(
        "/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers=_project(1)
    )
    assert response.status_code == 200
    assert response.json()["content"] == "an answer"


def test_an_empty_message_list_is_refused(client):
    _as(client, OWNER)
    assert client.post("/chat", json={"messages": []}, headers=_project(1)).status_code == 400


def test_history_requires_a_session(client):
    client.cookies.clear()
    assert client.get("/chat/history").status_code == 401
