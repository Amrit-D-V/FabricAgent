"""Scenario matrix for schema-based model routing.

FabricBI blocks with "which semantic model?" whenever two or more are connected,
and that gate stays — it exists because the assistant once inferred
`MavenMarket_Report` from a project description while `kaizen_v7` was connected.
The router only removes the blindness: it shows which model the question's own
words match, so the user is not guessing either.

Because the output sits next to a consent prompt, the failure that matters most
is a confident recommendation on thin evidence. Most of these tests are about
when the router should stay quiet.
"""

from __future__ import annotations

import json

import pytest

from llm.model_router import (
    _tokens,
    build_index,
    routing_hint,
    score_models,
)

KAIZEN = {
    "columns": [
        "vw_submissions.submission_id",
        "vw_submissions.zone",
        "vw_completion.completed_at",
        "Employee.employee_name",
    ],
    "measures": ["measures_.Kaizen Completed", "measures_.Total Submissions"],
    "workspace": "FabricBI-test",
}
SALES = {
    "columns": ["Sales.Amount", "Product.Category", "Date.Year", "Sales.ProductID"],
    "measures": ["Sales.Total Sales", "Sales.PY Sales"],
    "workspace": "FabricBI-test",
}
IDX = {"Kaizen": KAIZEN, "DIAD": SALES}


# --- Tokenisation -----------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("writtenPremium", {"written", "premium"}),
        ("written_premium", {"written", "premium"}),
        ("vw_submissions.submission_id", {"submissions", "submission"}),
        ("Sales.Total Sales", {"sales"}),
    ],
    ids=["camelCase", "snake_case", "dotted+underscore", "dedup"],
)
def test_identifiers_split_into_english_words(text, expected):
    """A question asked in English must match a column named in code."""
    assert _tokens(text) >= expected - {"id", "vw"}


def test_stopwords_are_not_matchable():
    """'total', 'show', 'report' appear in every model and discriminate nothing."""
    toks = _tokens("show me the total report data for all values")
    assert toks == set()


def test_short_words_are_ignored():
    assert _tokens("a b to of id") == set()


# --- Routing that should be confident ---------------------------------------


def test_a_question_routes_to_the_matching_model():
    scored = score_models(
        "how many kaizen submissions by zone?", ("Kaizen", "DIAD"), index=IDX
    )
    assert scored[0].model == "Kaizen"
    assert scored[0].score > scored[1].score


def test_the_other_question_routes_the_other_way():
    scored = score_models(
        "total sales by product category", ("Kaizen", "DIAD"), index=IDX
    )
    assert scored[0].model == "DIAD"


def test_the_hint_names_a_clear_winner_with_evidence():
    hint = routing_hint("kaizen submissions by zone", ("Kaizen", "DIAD"), index=IDX)
    assert "Best schema match: Kaizen" in hint
    assert "->" in hint, "must show WHICH fields matched, not just a score"


def test_the_hint_always_defers_to_the_user():
    """It sits directly beneath a consent prompt. It must not read as a decision."""
    hint = routing_hint("kaizen submissions by zone", ("Kaizen", "DIAD"), index=IDX)
    assert "not a decision" in hint.lower()


# --- Staying quiet ----------------------------------------------------------


def test_an_unmatchable_question_produces_no_hint():
    assert routing_hint("tell me a joke", ("Kaizen", "DIAD"), index=IDX) == ""


def test_an_empty_question_produces_no_hint():
    assert routing_hint("", ("Kaizen", "DIAD"), index=IDX) == ""


def test_a_single_weak_match_does_not_name_a_winner():
    """One incidental word is not routing evidence. Naming a winner on it stops
    the user reading the model list at all."""
    hint = routing_hint("what about zone", ("Kaizen", "DIAD"), index=IDX)
    assert "Best schema match" not in hint


def test_a_tie_does_not_name_a_winner():
    idx = {
        "A": {"columns": ["T.sales"], "measures": [], "workspace": ""},
        "B": {"columns": ["T.sales"], "measures": [], "workspace": ""},
    }
    hint = routing_hint("sales revenue figures", ("A", "B"), index=idx)
    assert "Best schema match" not in hint
    if hint:
        assert "A" in hint and "B" in hint


# --- Unknown is not zero ----------------------------------------------------


def test_a_model_with_no_cached_schema_is_unknown_not_zero():
    """'Nothing cached' and 'nothing matched' must not look alike — one is a
    missing refresh, the other is evidence."""
    scored = score_models("sales", ("DIAD", "NeverRefreshed"), index=IDX)
    unknown = next(s for s in scored if s.model == "NeverRefreshed")
    assert unknown.known is False
    assert "refresh" in unknown.note.lower()


def test_unscored_models_are_surfaced_in_the_hint():
    idx = dict(IDX)
    hint = routing_hint(
        "kaizen submissions by zone", ("Kaizen", "DIAD", "Ghost"), index=idx
    )
    assert "Ghost" in hint and "not ruled out" in hint


def test_an_entirely_unknown_model_set_produces_no_hint():
    assert routing_hint("sales", ("Ghost1", "Ghost2"), index={}) == ""


# --- Index building ---------------------------------------------------------


def _write(tmp_path, name, models, workspace, entries):
    payload = {"_meta": {"model_names": models, "workspace": workspace}}
    payload.update({e: True for e in entries})
    (tmp_path / name).write_text(json.dumps(payload), encoding="utf-8")


def test_the_index_is_keyed_by_model_name(tmp_path):
    _write(tmp_path, "column_abc.json", ["Kaizen"], "FabricBI-test", ["T.zone"])
    _write(tmp_path, "measure_abc.json", ["Kaizen"], "FabricBI-test", ["T.Total"])
    idx = build_index(str(tmp_path))
    assert set(idx) == {"Kaizen"}
    assert idx["Kaizen"]["columns"] == ["T.zone"]
    assert idx["Kaizen"]["measures"] == ["T.Total"]


def test_a_renamed_workspace_does_not_orphan_the_registry(tmp_path):
    """The filename fingerprint folds in the workspace NAME, so a rename makes
    the computed path miss a file that is still perfectly good — the same trap
    that already bit FabricConnection. Indexing by model_names avoids it."""
    _write(tmp_path, "column_oldfingerprint.json", ["Kaizen"], "FabricBI", ["T.zone"])
    idx = build_index(str(tmp_path))
    assert "Kaizen" in idx
    assert idx["Kaizen"]["workspace"] == "FabricBI"


def test_a_union_registry_is_not_usable_for_routing(tmp_path):
    """A file covering two models cannot attribute a field to either, so it must
    not be indexed — see HANDOVER open item 5."""
    _write(
        tmp_path, "column_union.json", ["Kaizen", "DIAD"], "FabricBI-test", ["T.zone"]
    )
    assert build_index(str(tmp_path)) == {}


def test_a_missing_directory_yields_an_empty_index():
    assert build_index("/no/such/dir/anywhere") == {}


def test_malformed_registry_json_is_skipped(tmp_path):
    (tmp_path / "column_bad.json").write_text("{not json", encoding="utf-8")
    _write(tmp_path, "column_ok.json", ["Kaizen"], "ws", ["T.zone"])
    assert set(build_index(str(tmp_path))) == {"Kaizen"}


def test_meta_keys_are_not_treated_as_fields(tmp_path):
    _write(tmp_path, "column_abc.json", ["Kaizen"], "ws", ["T.zone"])
    assert build_index(str(tmp_path))["Kaizen"]["columns"] == ["T.zone"]


def test_unrelated_files_are_ignored(tmp_path):
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    _write(tmp_path, "something_else.json", ["Kaizen"], "ws", ["T.zone"])
    assert build_index(str(tmp_path)) == {}


# --- The gate keeps working even if routing breaks --------------------------


def test_routing_failure_never_blocks_the_gate(monkeypatch):
    """The hint is a convenience bolted onto a security gate. If it throws, the
    gate must still return its question."""
    import llm.pbi_tool_exec as X

    def boom(*a, **k):
        raise RuntimeError("index corrupt")

    monkeypatch.setattr("llm.model_router.routing_hint", boom)
    assert X._routing_hint_block(("A", "B")) == ""


def test_the_hint_block_is_marked_advisory(monkeypatch):
    import llm.pbi_tool_exec as X

    monkeypatch.setattr("llm.model_router.routing_hint", lambda q, m, **k: "SOMETHING")
    monkeypatch.setattr(X, "_last_user_message", lambda: "kaizen")
    block = X._routing_hint_block(("A", "B"))
    assert "advisory" in block.lower()
