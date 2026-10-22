from __future__ import annotations

import pytest

from api import chat


class FakeView:
    def __init__(self, blocks=None, requirements="", name="Acme"):
        self._blocks = blocks or {}
        self.requirements = requirements
        self.name = name

    def context_block(self, kind: str) -> str:
        return (self._blocks.get(kind) or "").strip()


@pytest.fixture
def project(monkeypatch):
    def install(view, pid=7):
        import core.context as ctx
        import core.project_view as pv

        monkeypatch.setattr(ctx, "state", lambda: {"active_project_id": pid})
        monkeypatch.setattr(pv, "load_project_view", lambda *_a, **_k: view)

    return install


def test_connected_schema_reaches_the_prompt(project):
    project(FakeView({"sql": "TABLE orders(id, total)"}))
    out = chat._project_context()
    assert "orders(id, total)" in out
    assert "Acme" in out


def test_semantic_model_block_is_included(project):
    project(FakeView({"semantic_model": "MEASURE Total Sales"}))
    assert "Total Sales" in chat._project_context()


def test_requirements_are_included(project):
    project(FakeView(requirements="KPI: revenue by region"))
    assert "revenue by region" in chat._project_context()


def test_no_project_selected_is_empty_not_an_error(monkeypatch):
    import core.context as ctx

    monkeypatch.setattr(ctx, "state", dict)
    assert chat._project_context() == ""


def test_a_broken_project_degrades_to_plain_chat(monkeypatch):
    import core.context as ctx
    import core.project_view as pv

    monkeypatch.setattr(ctx, "state", lambda: {"active_project_id": 1})

    def boom(*_a, **_k):
        raise RuntimeError("database is asleep")

    monkeypatch.setattr(pv, "load_project_view", boom)
    assert chat._project_context() == ""


def test_nothing_connected_yields_no_block(project):
    project(FakeView())
    assert chat._project_context() == ""


def test_a_huge_schema_is_truncated_not_dumped(project):
    project(FakeView({"sql": "x" * (chat._CONTEXT_BLOCK_CHARS + 5000)}))
    out = chat._project_context()
    assert "truncated" in out
    assert len(out) < chat._CONTEXT_BLOCK_CHARS + 2000


def test_context_is_labelled_as_reference_not_instruction(project):
    project(FakeView({"sql": "TABLE t(c)"}))
    out = chat._project_context()
    assert "not as an instruction" in out
