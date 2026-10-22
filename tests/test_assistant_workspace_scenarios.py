"""Scenario matrix for the workspace the assistant is told to connect to.

The assistant's system prompt names a default workspace. When that name is
wrong every tool call fails with "workspace not found", which reads like a
permissions problem and is nearly impossible to diagnose from the chat.

Two ways it went wrong, both pinned here:

  * The prompt read FABRIC_WORKSPACE straight from the environment, while the
    saved connection in the database said something else. The .env value is
    only ever a fallback -- the stored connection wins.
  * The prompt was built once at import, so a workspace renamed in Fabric (or
    a connection edited in the UI) stayed wrong until the process restarted.
"""

from __future__ import annotations

import pytest

from api import chat


@pytest.fixture
def workspace(monkeypatch):
    """Point the resolver at a known name, the way a saved connection would."""

    def use(name: str, models: list[str] | None = None):
        monkeypatch.setattr(chat, "_active_workspace", lambda: name)
        monkeypatch.setattr(chat, "_active_models", lambda: models or [])

    return use


# --- The prompt names the resolved workspace, not the env var ---------------


def test_the_prompt_names_the_resolved_workspace(workspace):
    workspace("FabricBI-test")
    assert "'FabricBI-test'" in chat.system_prompt("dashboard")


def test_both_modes_name_it(workspace):
    workspace("FabricBI-test")
    for mode in ("dashboard", "general"):
        assert "'FabricBI-test'" in chat.system_prompt(mode), mode


def test_a_stale_env_var_does_not_reach_the_prompt(workspace, monkeypatch):
    """FABRIC_WORKSPACE in .env is a fallback for when there is no saved
    connection. It must never override one that exists."""
    monkeypatch.setenv("FABRIC_WORKSPACE", "FabricBI")
    workspace("FabricBI-test")
    prompt = chat.system_prompt("dashboard")
    assert "'FabricBI-test'" in prompt
    assert "'FabricBI'." not in prompt


def test_an_unknown_mode_falls_back_to_dashboard(workspace):
    workspace("FabricBI-test")
    assert chat.system_prompt("nonsense") == chat.system_prompt("dashboard")


# --- It is resolved per request, not once at import -------------------------


def test_a_renamed_workspace_is_picked_up_without_a_restart(workspace):
    """Power BI reports a renamed workspace as 'not found', so a prompt cached
    at import time keeps sending the assistant somewhere that no longer exists."""
    workspace("Old Name")
    assert "'Old Name'" in chat.system_prompt("dashboard")
    workspace("New Name")
    assert "'New Name'" in chat.system_prompt("dashboard")


def test_health_reports_the_resolved_workspace(workspace, monkeypatch):
    workspace("FabricBI-test", ["kaizen_v7"])
    monkeypatch.setattr(chat, "get_provider", lambda agent: "claude_cli")
    monkeypatch.setattr(chat, "get_model_name", lambda agent: "test-model")
    info = chat.health_info()
    assert info["workspace"] == "FabricBI-test"
    assert info["models"] == ["kaizen_v7"]


# --- The models are named, so the assistant need not stop and ask -----------


def test_the_known_models_are_listed(workspace):
    workspace("FabricBI-test", ["kaizen_v7", "MavenMarket_Report"])
    prompt = chat.system_prompt("dashboard")
    assert "'kaizen_v7'" in prompt
    assert "'MavenMarket_Report'" in prompt


def test_no_models_means_no_hint_rather_than_an_empty_list(workspace):
    """A connection with no models listed must not produce 'Semantic models
    in it: .' -- that reads as 'there are none', which is different."""
    workspace("FabricBI-test", [])
    assert "Semantic models in it" not in chat.system_prompt("dashboard")


def test_the_model_list_is_capped(workspace):
    """A workspace with hundreds of models must not crowd out the prompt."""
    workspace("Big", [f"model_{i}" for i in range(50)])
    prompt = chat.system_prompt("dashboard")
    assert "'model_0'" in prompt
    assert "'model_49'" not in prompt


def test_a_model_name_with_an_apostrophe_does_not_break_the_listing(workspace):
    workspace("FabricBI-test", ["Bob's Report"])
    assert "Bob's Report" in chat.system_prompt("dashboard")
