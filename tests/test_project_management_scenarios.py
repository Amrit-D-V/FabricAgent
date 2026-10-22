"""Scenario matrix for the Project Management API: connected sources, the KPI
binding flow, the per-project LLM, and document deletion.

These were ui/source_cards.py, ui/requirements_panel.py and the LLM section of
pages/12_Project_Management.py. The operations all lived in db/ and model/
already; what the routes add is the ordering (probe before save; never write a
blank password over a saved one) and the scoping (a source or a document
belongs to the active project, never to an id the client picked).

Everything here runs offline: the probes, the LLM and the database writes are
faked at the module boundary, so what is pinned is the routes' own behaviour.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from auth.session_cookie import COOKIE_NAME, serialize_session
from core.project_view import ProjectView
from model.project_requirements import open_questions, parse_spec
from model.project_sources import SourceProbe

ADMIN = {"email": "admin@fabricbi.example", "role": "admin", "is_active": 1}
EDITOR = {"email": "editor@fabricbi.example", "role": "editor", "is_active": 1}
VIEWER = {"email": "viewer@fabricbi.example", "role": "viewer", "is_active": 1}

PROJECT = {"X-FabricBI-Project": "1"}

SPEC = """# Objective
Track sales.

## Key Metrics / KPIs
- Revenue — total sales amount
- Margin — profit over revenue — Sales.Margin

## Open Questions
- Which fiscal calendar applies?
- **Currency** — which reporting currency?
"""


class _P:
    def __init__(self, pid=1, name="Alpha"):
        self.id = pid
        self.name = name
        self.description = ""
        self.client_id = 1
        self.status = "active"


def _events(response) -> dict:
    """The last payload per SSE event name -- `done` or `error` is what matters."""
    out = {}
    for frame in response.text.split("\n\n"):
        event = re.search(r"^event: (.+)$", frame, re.M)
        data = re.search(r"^data: (.+)$", frame, re.M)
        if event and data:
            out[event.group(1)] = json.loads(data.group(1))
    return out


@pytest.fixture
def client(monkeypatch):
    table = {u["email"]: dict(u) for u in (ADMIN, EDITOR, VIEWER)}
    monkeypatch.setattr("auth.auth.get_app_user", lambda e: table.get(e))
    monkeypatch.setattr("db.database.get_app_user", lambda e: table.get(e))
    monkeypatch.setattr(
        "auth.auth._cached_projects_for_user", lambda email, is_admin: [_P()]
    )
    monkeypatch.setattr("auth.auth.active_workspace_id", lambda: "")
    monkeypatch.setattr("db.graph_memory.enabled", lambda: False)
    monkeypatch.setattr("db.database.audit", lambda *a, **k: None)
    from api.server import app

    return TestClient(app)


def _as(client: TestClient, user: dict) -> TestClient:
    client.cookies.set(
        COOKIE_NAME, serialize_session(user["email"], user["role"], user["email"])
    )
    return client


@pytest.fixture
def editor(client):
    return _as(client, EDITOR)


@pytest.fixture
def view(monkeypatch):
    """The active project's snapshot -- call it to swap in metadata."""
    holder = {}

    def make(metadata=None, documents=()):
        holder["view"] = ProjectView(
            project=_P(),
            documents=list(documents),
            metadata=metadata or {},
            user_email=EDITOR["email"],
        )
        return holder["view"]

    make()
    monkeypatch.setattr(
        "core.project_view.load_project_view", lambda pid, user_email="": holder["view"]
    )
    return make


@pytest.fixture
def audited(monkeypatch):
    actions: list[str] = []
    monkeypatch.setattr("db.database.audit", lambda action, **k: actions.append(action))
    return actions


# --- Who may touch a source --------------------------------------------------


def test_sources_need_a_session(client):
    assert client.get("/project/sources", headers=PROJECT).status_code == 401


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/project/sources"),
        ("POST", "/project/sources/sql"),
        ("DELETE", "/project/sources/sql"),
        ("POST", "/project/requirements/bind"),
        ("POST", "/project/requirements/suggest"),
        ("POST", "/project/llm/test"),
        ("DELETE", "/documents/1"),
    ],
)
def test_a_viewer_may_not_change_the_project(client, method, path):
    _as(client, VIEWER)
    assert client.request(method, path, headers=PROJECT).status_code == 403


def test_a_source_needs_an_active_project(editor, view):
    """No header, no project: the page cannot guess which one was meant."""
    assert editor.get("/project/sources").status_code == 404


def test_only_the_two_known_kinds_exist(editor, view):
    assert editor.delete("/project/sources/warehouse", headers=PROJECT).status_code == 404


# --- Reading the state -------------------------------------------------------


def _sql_config(**over):
    cfg = {
        "server": "wh.fabric",
        "database": "Sales",
        "auth_mode": "SQL Username + Password",
        "username": "reader",
        "password_encrypted": "gAAAA-token",
    }
    cfg.update(over)
    return cfg


def test_secrets_never_leave_the_server(editor, view):
    view({"sources": {"sql": _sql_config()}, "context_blocks": {"sql": "## schema"}})
    sql = editor.get("/project/sources", headers=PROJECT).json()["sources"]["sql"]
    assert sql["config"]["password_set"] is True
    assert "password" not in sql["config"]
    assert "password_encrypted" not in sql["config"]
    assert sql["captured"] is True


def test_a_saved_but_unread_source_is_reported_as_such(editor, view):
    """The address is on record while the capture never completed: the page
    must be able to say so rather than show it as 'not connected'."""
    view({"sources": {"sql": _sql_config()}})
    sql = editor.get("/project/sources", headers=PROJECT).json()["sources"]["sql"]
    assert sql["config"] is not None
    assert sql["captured"] is False


def test_an_unconfigured_source_has_no_config(editor, view):
    body = editor.get("/project/sources", headers=PROJECT).json()
    assert body["sources"]["semantic_model"] == {
        "config": None,
        "captured": False,
        "preview": "",
        "length": 0,
    }


def test_the_preview_is_capped_but_the_length_is_honest(editor, view):
    view({"sources": {"sql": _sql_config()}, "context_blocks": {"sql": "x" * 25_000}})
    sql = editor.get("/project/sources", headers=PROJECT).json()["sources"]["sql"]
    assert len(sql["preview"]) == 20_000
    assert sql["length"] == 25_000


def test_the_column_catalog_merges_both_sources(editor, view):
    view(
        {
            "context_blocks": {
                "sql__columns": json.dumps([{"name": "dbo.Sales", "columns": ["Amount"]}]),
                "semantic_model__columns": json.dumps(
                    [{"name": "Date", "columns": ["Year", "Month"]}]
                ),
            }
        }
    )
    body = editor.get("/project/sources", headers=PROJECT).json()
    assert body["columns"] == ["Date.Month", "Date.Year", "dbo.Sales.Amount"]


def test_feeding_names_what_the_extraction_will_read(editor, view):
    view(
        {"sources": {"sql": _sql_config()}, "context_blocks": {"sql": "## schema"}},
        documents=[SimpleNamespace(id=1)],
    )
    body = editor.get("/project/sources", headers=PROJECT).json()
    assert body["feeding"] == ["1 document(s)", "database schema"]


def test_the_auth_modes_are_the_real_ones(editor, view):
    from fabric.fabric_sql import AUTH_AAD_TOKEN

    body = editor.get("/project/sources", headers=PROJECT).json()
    assert body["authModes"][0] == AUTH_AAD_TOKEN
    assert len(body["authModes"]) == 4


@pytest.mark.parametrize("mode", ["aad_token", "service_principal", "aad_password", "sql"])
def test_chat_connector_auth_ids_are_normalized_before_probe(editor, probes, saved, mode):
    from fabric.fabric_sql import AUTH_MODES_BY_ID

    response = editor.post("/project/sources/sql", headers=PROJECT, json={
        "server": "server", "database": "database", "auth_mode": mode,
        "username": "reader", "password": "secret",
    })
    assert "done" in _events(response)
    assert probes["sql"]["auth_mode"] == AUTH_MODES_BY_ID[mode]
    assert saved["source"][1]["auth_mode"] == AUTH_MODES_BY_ID[mode]


def test_invalid_auth_id_is_rejected_before_probe_or_save(editor, probes, saved):
    response = editor.post("/project/sources/sql", headers=PROJECT, json={
        "server": "server", "database": "database", "auth_mode": "typo",
    })
    assert response.status_code == 400
    assert probes == {} and saved["source"] is None


# --- Connecting --------------------------------------------------------------


@pytest.fixture
def probes(monkeypatch):
    """Both probes succeed and record what they were handed."""
    calls = {}

    def sql(server, database, auth_mode, username="", password="", profile=True):
        calls["sql"] = dict(
            server=server,
            database=database,
            auth_mode=auth_mode,
            username=username,
            password=password,
            profile=profile,
        )
        return SourceProbe(
            ok=True,
            summary="3 tables",
            block="## schema",
            tables=[{"name": "dbo.Sales", "columns": ["Amount"]}],
        )

    def semantic_model(server, database):
        calls["semantic_model"] = dict(server=server, database=database)
        return SourceProbe(
            ok=True,
            summary="2 tables, 4 measures",
            block="## model",
            server="powerbi://api.powerbi.com/v1.0/myorg/WS",
        )

    monkeypatch.setattr("model.project_sources.probe_sql_source", sql)
    monkeypatch.setattr("model.project_sources.probe_semantic_model", semantic_model)
    return calls


@pytest.fixture
def saved(monkeypatch):
    """What the connect flow writes, and the credential already on record."""
    writes = {"source": None, "blocks": {}, "cleared": None, "stored_password": "hunter2"}

    monkeypatch.setattr(
        "db.database.set_project_source",
        lambda pid, kind, config, user_email="": writes.__setitem__(
            "source", (kind, dict(config), user_email)
        ),
    )
    monkeypatch.setattr(
        "db.database.set_project_context_block",
        lambda pid, kind, text: writes["blocks"].__setitem__(kind, text),
    )
    monkeypatch.setattr(
        "db.database.get_project_source",
        lambda pid, kind, include_secrets=False: {
            "server": "wh.fabric",
            "database": "Sales",
            "password": writes["stored_password"] if include_secrets else "",
        },
    )
    monkeypatch.setattr(
        "db.database.clear_project_source",
        lambda pid, kind, user_email="": writes.__setitem__("cleared", kind),
    )
    monkeypatch.setattr("db.graph_memory.remember_source", lambda *a, **k: None)
    return writes


SQL_BODY = {
    "server": "wh.fabric",
    "database": "Sales",
    "auth_mode": "SQL Username + Password",
    "username": "reader",
    "password": "new-secret",
    "profile": False,
}


def test_missing_fields_are_refused_before_any_probe(editor, view, probes, saved):
    response = editor.post("/project/sources/sql", json={"server": "wh"}, headers=PROJECT)
    assert response.status_code == 400
    assert "required" in response.json()["error"]
    assert probes == {}


def test_a_typed_password_is_used_and_stored(editor, view, probes, saved):
    events = _events(editor.post("/project/sources/sql", json=SQL_BODY, headers=PROJECT))
    assert events["done"]["summary"] == "3 tables"
    assert probes["sql"]["password"] == "new-secret"
    assert probes["sql"]["profile"] is False
    kind, config, _ = saved["source"]
    assert kind == "sql"
    assert config["password"] == "new-secret"
    assert config["auth_mode"] == SQL_BODY["auth_mode"]


def test_the_actor_survives_the_worker_thread(editor, view, probes, saved):
    """The probe runs off the request thread, where the request scope is not
    visible -- the audit entry must still carry who connected."""
    _events(editor.post("/project/sources/sql", json=SQL_BODY, headers=PROJECT))
    assert saved["source"][2] == EDITOR["email"]


def test_the_schema_and_column_catalog_are_written_together(editor, view, probes, saved):
    _events(editor.post("/project/sources/sql", json=SQL_BODY, headers=PROJECT))
    assert saved["blocks"]["sql"] == "## schema"
    assert json.loads(saved["blocks"]["sql__columns"]) == [
        {"name": "dbo.Sales", "columns": ["Amount"]}
    ]


def test_a_blank_password_keeps_the_saved_one(editor, view, probes, saved):
    """Blank means 'unchanged': the saved credential opens the connection, and
    it is omitted from the write so the store keeps what it has."""
    body = {**SQL_BODY, "password": ""}
    events = _events(editor.post("/project/sources/sql", json=body, headers=PROJECT))
    assert "done" in events
    assert probes["sql"]["password"] == "hunter2"
    assert "password" not in saved["source"][1]


def test_a_missing_auth_mode_falls_back_to_the_default(editor, view, probes, saved):
    from fabric.fabric_sql import AUTH_AAD_TOKEN

    body = {**SQL_BODY, "auth_mode": ""}
    _events(editor.post("/project/sources/sql", json=body, headers=PROJECT))
    assert probes["sql"]["auth_mode"] == AUTH_AAD_TOKEN
    assert saved["source"][1]["auth_mode"] == AUTH_AAD_TOKEN, "stored resolved, not blank"


def test_the_address_that_connected_is_what_is_stored(editor, view, probes, saved):
    """A pasted portal URL is rewritten by the probe; the rewrite is kept."""
    body = {"server": "https://app.fabric.microsoft.com/groups/x", "database": "Model"}
    events = _events(
        editor.post("/project/sources/semantic_model", json=body, headers=PROJECT)
    )
    assert events["done"]["server"] == "powerbi://api.powerbi.com/v1.0/myorg/WS"
    kind, config, _ = saved["source"]
    assert kind == "semantic_model"
    assert config == {
        "server": "powerbi://api.powerbi.com/v1.0/myorg/WS",
        "database": "Model",
    }


def test_a_failed_probe_stores_nothing(editor, view, saved, monkeypatch):
    monkeypatch.setattr(
        "model.project_sources.probe_sql_source",
        lambda *a, **k: SourceProbe(error="Login failed for user 'reader'."),
    )
    events = _events(editor.post("/project/sources/sql", json=SQL_BODY, headers=PROJECT))
    assert events["error"]["message"] == "Login failed for user 'reader'."
    assert "done" not in events
    assert saved["source"] is None
    assert saved["blocks"] == {}


def test_disconnecting_clears_the_kind_asked_for(editor, view, saved):
    assert editor.delete("/project/sources/semantic_model", headers=PROJECT).status_code == 200
    assert saved["cleared"] == "semantic_model"


# --- Binding KPIs and settling questions -------------------------------------


@pytest.fixture
def spec_store(monkeypatch):
    """The saved spec, with the revision guard the real store applies."""
    store = {"text": SPEC, "rev": "r1", "writes": []}
    monkeypatch.setattr("db.database.get_project_requirements", lambda pid: store["text"])

    def save(pid, text, expected_rev=None):
        if expected_rev and expected_rev != store["rev"]:
            from db.database import StaleWriteError

            raise StaleWriteError("stale")
        store["text"] = text
        store["rev"] = "r2"
        store["writes"].append(text)
        return "r2"

    monkeypatch.setattr("db.database.set_project_requirements", save)
    return store


def test_the_spec_exposes_open_questions_with_their_markup(editor, view, spec_store):
    body = editor.get("/project/requirements", headers=PROJECT).json()
    keys = [q["question"] for q in body["openQuestions"]]
    assert "Which fiscal calendar applies?" in keys
    # The match key is the set-stripped form; the display text keeps the bold.
    shown = next(q for q in body["openQuestions"] if "Currency" in q["text"])
    assert shown["text"].startswith("**Currency**")


def test_binding_rewrites_the_kpi_line(editor, view, spec_store):
    response = editor.post(
        "/project/requirements/bind",
        json={"kpi": "Revenue", "column": "dbo.Sales.Amount", "revision": "r1"},
        headers=PROJECT,
    )
    assert response.status_code == 200
    kpis = {k["name"]: k for k in parse_spec(response.json()["text"])["kpis"]}
    assert kpis["Revenue"]["backing"] == "dbo.Sales.Amount"
    assert kpis["Margin"]["backing"] == "Sales.Margin", "the other line is untouched"
    assert response.json()["revision"] == "r2"


def test_binding_an_unknown_kpi_is_refused_not_silently_saved(editor, view, spec_store):
    response = editor.post(
        "/project/requirements/bind",
        json={"kpi": "Nope", "column": "dbo.Sales.Amount", "revision": "r1"},
        headers=PROJECT,
    )
    assert response.status_code == 400
    assert "Could not find" in response.json()["error"]
    assert spec_store["writes"] == []


def test_a_stale_revision_is_refused(editor, view, spec_store):
    response = editor.post(
        "/project/requirements/bind",
        json={"kpi": "Revenue", "column": "dbo.Sales.Amount", "revision": "old"},
        headers=PROJECT,
    )
    assert response.status_code == 400
    assert spec_store["writes"] == []


def test_binding_needs_both_halves(editor, view, spec_store):
    response = editor.post(
        "/project/requirements/bind", json={"kpi": "Revenue"}, headers=PROJECT
    )
    assert response.status_code == 400


def test_answering_records_a_decision(editor, view, spec_store, audited):
    response = editor.post(
        "/project/requirements/resolve",
        json={
            "question": "Which fiscal calendar applies?",
            "answer": "July to June",
            "revision": "r1",
        },
        headers=PROJECT,
    )
    assert response.status_code == 200
    text = response.json()["text"]
    assert "July to June" in text
    assert all("fiscal" not in k.lower() for k, _ in open_questions(text))
    assert audited[-1] == "requirements.question.resolve"


def test_dismissing_records_no_answer(editor, view, spec_store, audited):
    """An empty answer removes the question without inventing a decision."""
    response = editor.post(
        "/project/requirements/resolve",
        json={"question": "Which fiscal calendar applies?", "answer": "", "revision": "r1"},
        headers=PROJECT,
    )
    assert response.status_code == 200
    remaining = [k for k, _ in open_questions(response.json()["text"])]
    assert "Which fiscal calendar applies?" not in remaining
    assert len(remaining) == 1
    assert audited[-1] == "requirements.question.dismiss"


def test_suggesting_needs_captured_columns(editor, view, spec_store):
    view({"requirements": SPEC})
    response = editor.post("/project/requirements/suggest", json={}, headers=PROJECT)
    assert response.status_code == 400
    assert "Connect a database" in response.json()["error"]


def test_suggestions_are_offered_not_written(editor, view, spec_store, monkeypatch):
    view(
        {
            "requirements": SPEC,
            "context_blocks": {
                "sql__columns": json.dumps([{"name": "Sales", "columns": ["Amount"]}])
            },
        }
    )
    asked = {}

    def suggest(kpis, columns, agent="default"):
        asked["kpis"] = [k["name"] for k in kpis]
        asked["columns"] = columns
        return {"Revenue": "Sales.Amount"}

    monkeypatch.setattr("model.project_requirements.suggest_kpi_bindings", suggest)
    events = _events(editor.post("/project/requirements/suggest", json={}, headers=PROJECT))
    assert events["done"]["matches"] == {"Revenue": "Sales.Amount"}
    assert asked["kpis"] == ["Revenue"], "only the unbacked KPI is put to the model"
    assert asked["columns"] == ["Sales.Amount"]
    assert spec_store["writes"] == [], "a suggestion is a pre-selection, not a write"


def test_clarifying_questions_stream_a_done_event(editor, view, monkeypatch):
    """stream() needs the running loop, which a threadpool handler does not
    have -- this route has to be async or it 500s."""
    monkeypatch.setattr(
        "model.project_requirements.ask_clarifying_questions", lambda pid: ["Which region?"]
    )
    events = _events(editor.post("/project/requirements/questions", json={}, headers=PROJECT))
    assert events["done"]["questions"] == ["Which region?"]


# --- The per-project LLM -----------------------------------------------------


def test_each_agent_reports_what_it_resolves_to(editor, view, monkeypatch):
    monkeypatch.setattr(
        "db.database.get_project_llm_config",
        lambda pid: {"provider": "anthropic", "model": "claude-x"},
    )
    monkeypatch.setattr("llm.llm_client.get_provider", lambda agent="default": "anthropic")
    monkeypatch.setattr("llm.llm_client.get_model_name", lambda agent="default": "claude-x")
    body = editor.get("/project/llm", headers=PROJECT).json()
    assert body["provider"] == "anthropic"
    assert [a["key"] for a in body["agents"]] == [
        "semantic_model",
        "chat",
        "dax",
        "dashboard",
    ]
    assert all(a["model"] == "claude-x" for a in body["agents"])


def test_no_pin_is_reported_as_empty_strings(editor, view, monkeypatch):
    monkeypatch.setattr("db.database.get_project_llm_config", lambda pid: None)
    monkeypatch.setattr("llm.llm_client.get_provider", lambda agent="default": "ollama")
    monkeypatch.setattr("llm.llm_client.get_model_name", lambda agent="default": "llama")
    body = editor.get("/project/llm", headers=PROJECT).json()
    assert body["provider"] == ""
    assert body["agents"][0]["provider"] == "ollama"


def test_a_test_needs_both_halves(editor, view):
    response = editor.post("/project/llm/test", json={"provider": "anthropic"}, headers=PROJECT)
    assert response.status_code == 400


def test_a_successful_test_reports_the_reply_and_timing(editor, view, monkeypatch):
    seen = {}

    def fake_stream(system, user, **kwargs):
        seen.update(kwargs)
        return iter(["O", "K"])

    monkeypatch.setattr("llm.llm_client.stream_text", fake_stream)
    events = _events(
        editor.post(
            "/project/llm/test", json={"provider": "anthropic", "model": "m"}, headers=PROJECT
        )
    )
    assert events["done"]["text"] == "OK"
    assert events["done"]["ms"] >= 0
    assert (seen["provider"], seen["model"]) == ("anthropic", "m"), "tries the pair asked for"


def test_a_failed_test_is_an_error_event_not_a_500(editor, view, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("401 invalid key")

    monkeypatch.setattr("llm.llm_client.stream_text", boom)
    monkeypatch.setattr(
        "llm.llm_client.friendly_provider_error", lambda p, m, e: f"friendly: {e}"
    )
    events = _events(
        editor.post(
            "/project/llm/test", json={"provider": "anthropic", "model": "m"}, headers=PROJECT
        )
    )
    assert events["error"]["message"] == "friendly: 401 invalid key"


# --- Documents ---------------------------------------------------------------


@pytest.fixture
def documents(monkeypatch):
    deleted = []
    docs = {7: SimpleNamespace(id=7, project_id=1), 8: SimpleNamespace(id=8, project_id=2)}
    monkeypatch.setattr("db.database.get_document", lambda i: docs.get(i))
    monkeypatch.setattr("db.database.delete_document", lambda i: deleted.append(i))
    return deleted


def test_a_document_in_the_active_project_is_deleted(editor, documents):
    assert editor.delete("/documents/7", headers=PROJECT).status_code == 200
    assert documents == [7]


def test_a_document_from_another_project_is_not_found(editor, documents):
    """The id is client-supplied; membership is what decides, not existence."""
    assert editor.delete("/documents/8", headers=PROJECT).status_code == 404
    assert documents == []


def test_a_missing_document_is_not_found(editor, documents):
    assert editor.delete("/documents/99", headers=PROJECT).status_code == 404
