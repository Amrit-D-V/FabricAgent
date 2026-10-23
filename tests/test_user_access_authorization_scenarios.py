from __future__ import annotations

import pytest

from llm import pbi_tool_exec as ex

ME = "developer@example.com"
SOMEONE = "colleague@example.com"


@pytest.fixture
def caller(monkeypatch):
    def install(email=ME, role="editor", answer=None):
        monkeypatch.setenv("FABRICBI_CALLER_EMAIL", email)
        monkeypatch.setenv("FABRICBI_CALLER_ROLE", role)
        called: dict = {}

        def fake_access_for(user_id, **kw):
            called["user_id"] = user_id
            return answer or {"ok": True, "user": user_id, "source": "delegated",
                              "summary": {}, "workspaces": [], "items": {}, "notes": []}

        monkeypatch.setattr(
            "fabric.admin_user_access.access_for", fake_access_for, raising=True
        )
        monkeypatch.setattr(
            "fabric.admin_user_access.cli_tokens", lambda: ("t", "f"), raising=True
        )
        monkeypatch.setattr(
            "fabric.admin_user_access.token_upn", lambda t: "admin@example.com" if t else "", raising=False
        )
        monkeypatch.setattr(
            "auth.access_policy.access_for", fake_access_for, raising=False
        )
        monkeypatch.setattr(
            "auth.access_policy.cli_tokens", lambda: ("t", "f"), raising=False
        )
        monkeypatch.setattr(
            "auth.access_policy.token_upn", lambda t: "admin@example.com" if t else "", raising=False
        )
        monkeypatch.setattr(
            "auth.access_policy._role_from_app_users",
            lambda email: "admin" if email == "admin@example.com" else "viewer",
            raising=False,
        )
        return called

    return install


def run(user_id: str) -> str:
    return ex._execute_pbi_tool_impl("pbi_user_access", {"userId": user_id})


def test_a_non_admin_cannot_look_up_a_colleague(caller):
    called = caller(role="editor")
    out = run(SOMEONE)
    assert out.startswith("REFUSED")
    assert "restricted to" in out
    assert not called, "the lookup must not run at all"


def test_a_viewer_cannot_either(caller):
    caller(role="viewer")
    assert run(SOMEONE).startswith("REFUSED")


def test_a_non_admin_can_still_look_up_themselves(caller):
    called = caller(role="editor")
    out = run(ME)
    assert not out.startswith("REFUSED")
    assert called["user_id"] == ME


def test_case_and_whitespace_do_not_bypass_the_gate(caller):
    caller(role="editor")
    assert not run(f"  {ME.upper()}  ").startswith("REFUSED")


def test_an_admin_may_look_up_anyone(caller):
    called = caller(role="admin")
    out = run(SOMEONE)
    assert not out.startswith("REFUSED")
    assert called["user_id"] == SOMEONE


def test_the_refusal_names_the_caller_so_it_is_actionable(caller):
    caller(role="editor")
    assert ME in run(SOMEONE)


def test_no_caller_identity_does_not_block_the_cli(caller, monkeypatch):
    called = caller(role="editor")
    monkeypatch.delenv("FABRICBI_CALLER_EMAIL", raising=False)
    monkeypatch.delenv("FABRICBI_CALLER_ROLE", raising=False)
    assert not run(SOMEONE).startswith("REFUSED")
    assert called["user_id"] == SOMEONE


def test_role_is_read_fresh_each_call(caller, monkeypatch):
    caller(role="admin")
    assert not run(SOMEONE).startswith("REFUSED")
    monkeypatch.setenv("FABRICBI_CALLER_ROLE", "editor")
    assert run(SOMEONE).startswith("REFUSED")
