"""Role enforcement at the MCP boundary, independent of what the LLM asks for."""

import json
from types import SimpleNamespace

import pytest

from api import chat
from auth import tool_access
from core import context
from llm import pbi_tool_exec as executor
from llm.mcp_tools import make_handler
from llm.pbi_tool_specs import PBI_TOOLS


@pytest.fixture
def caller(monkeypatch):
    token = context.begin()
    for key in (
        "FABRICBI_CALLER_EMAIL",
        "FABRICBI_CALLER_ROLE",
        "FABRICBI_CALLER_PROJECT",
        "FABRICBI_CALLER_READ_ONLY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FABRICBI_CALLER_EMAIL", "viewer@example.test")
    monkeypatch.setenv("FABRICBI_CALLER_ROLE", "viewer")
    monkeypatch.setattr(executor, "_build_trace", lambda *args, **kwargs: None)
    yield
    context.end(token)


@pytest.mark.parametrize(
    "name",
    [s["name"] for s in PBI_TOOLS if s["name"] not in tool_access.READ_ONLY_TOOLS]
    + ["future_write_tool"],
)
def test_viewer_mutation_is_refused_before_dispatch(caller, monkeypatch, name):
    def forbidden(*args):
        pytest.fail("A viewer reached the mutation implementation")

    monkeypatch.setattr(executor, "_execute_pbi_tool_impl", forbidden)
    assert make_handler(name)().startswith("REFUSED")


@pytest.mark.parametrize("role", ["viewer", "", "unexpected"])
def test_unknown_or_missing_role_fails_closed(caller, monkeypatch, role):
    monkeypatch.setenv("FABRICBI_CALLER_ROLE", role)
    assert not tool_access.tool_allowed("pbi_paginated_delete")


@pytest.mark.parametrize("role", ["editor", "admin"])
def test_editor_mutations_remain_available(caller, monkeypatch, role):
    monkeypatch.setenv("FABRICBI_CALLER_ROLE", role)
    called = []
    monkeypatch.setattr(
        executor, "_execute_pbi_tool_impl", lambda n, a: called.append(n) or "ok"
    )
    assert make_handler("pbi_paginated_delete")() == "ok"
    assert called == ["pbi_paginated_delete"]


def test_query_tools_still_work_for_viewers(caller, monkeypatch):
    monkeypatch.setattr(executor, "_execute_pbi_tool_impl", lambda n, a: "queried")
    assert make_handler("pbi_dax_query")(dax='EVALUATE ROW("Total", 1)') == "queried"


def test_general_mode_cannot_be_promoted_by_editor_role(caller, monkeypatch):
    monkeypatch.setenv("FABRICBI_CALLER_ROLE", "editor")
    monkeypatch.setenv("FABRICBI_CALLER_READ_ONLY", "1")
    assert not tool_access.tool_allowed("pbi_paginated_delete")


@pytest.mark.parametrize(
    "role,mode,readonly",
    [
        ("viewer", "dashboard", True),
        ("editor", "dashboard", False),
        ("admin", "dashboard", False),
        ("editor", "general", True),
    ],
)
def test_chat_launches_only_role_appropriate_servers(monkeypatch, role, mode, readonly):
    token = context.begin({"email": "user@example.test", "role": role})
    context.state()["active_project_id"] = 1
    project = SimpleNamespace(id=1, name="Client One")
    monkeypatch.setattr("auth.auth.visible_projects", lambda: [project])
    monkeypatch.setattr(chat, "_caller_role", lambda: role)
    monkeypatch.setattr(chat, "_active_workspace", lambda: "Workspace")
    monkeypatch.setattr(chat, "_active_models", lambda: ["Model"])
    monkeypatch.setattr(chat, "_project_context", lambda: "")
    monkeypatch.setattr(chat, "get_provider", lambda a: "claude_cli")
    monkeypatch.setattr(chat, "get_model_name", lambda a: "offline")
    monkeypatch.setattr(chat, "_drain_visuals", lambda sid: [])
    captured = {}

    def stream(*args, **kwargs):
        captured.update(kwargs)
        yield "Answer"

    monkeypatch.setattr(chat, "stream_text", stream)
    try:
        assert (
            chat.run_chat([{"role": "user", "content": "hi"}], mode)["content"]
            == "Answer"
        )
        servers = json.loads(captured["mcp_config"])["mcpServers"]
        assert ("powerbi_modeling" not in servers) == readonly
        assert ("mcp__powerbi_modeling" not in captured["allowed_tools"]) == readonly
        assert captured["extra_env"]["FABRICBI_CALLER_READ_ONLY"] == (
            "1" if readonly else "0"
        )
        assert captured["extra_env"]["FABRICBI_CALLER_PROJECT"] == "Client One"
        assert captured["strict_mcp"] is True
    finally:
        context.end(token)


def test_request_role_uses_fabric_cap(monkeypatch):
    token = context.begin({"email": "editor@example.test", "role": "editor"})
    monkeypatch.setattr("auth.auth.effective_role", lambda: "viewer")
    try:
        assert not tool_access.tool_allowed("pbi_paginated_delete")
    finally:
        context.end(token)
