"""Scenario matrix for model targeting and the registry bind guard.

Two guards sit in front of every pbi_* tool that touches a semantic model:

  _resolve_target_model  -- never silently pick models[0] when several are
                            connected, because acting on the wrong model
                            produces real-looking wrong numbers.
  _model_choice_gate     -- naming a model in a tool call is not consent. The
                            gate clears only when a NEW user message arrives,
                            so the assistant cannot answer on the user's behalf
                            by retrying inside the same turn.

Then _validate_bind_fields refuses to pass a hallucinated Table[Field] through
to pbi-cli. Each is exercised across its whole decision space here, including
the retry-in-the-same-turn path that motivated the gate.
"""

from __future__ import annotations

import pytest

import llm.pbi_tool_exec as X


@pytest.fixture
def models(monkeypatch):
    """Control what _active_models() reports, per scenario."""
    state = {"models": ()}
    monkeypatch.setattr(X, "_active_models", lambda: state["models"])
    return state


@pytest.fixture
def turns(monkeypatch):
    """Control the user-message count the gate keys off."""
    state = {"n": None}
    monkeypatch.setattr(X, "_user_turn_count", lambda: state["n"])
    return state


# --- _resolve_target_model: choosing without a request ----------------------


def test_nothing_connected_is_an_explicit_error(models, turns):
    models["models"] = ()
    name, err = X._resolve_target_model(None)
    assert name == ""
    assert "no semantic model connected" in err


def test_a_single_connected_model_needs_no_request(models, turns):
    models["models"] = ("only_model",)
    assert X._resolve_target_model(None) == ("only_model", "")


def test_several_connected_models_refuse_to_guess(models, turns):
    """The failure this prevents: defaulting to models[0] and returning
    confident numbers from the wrong model."""
    models["models"] = ("a", "b", "c")
    name, err = X._resolve_target_model(None)
    assert name == ""
    assert "`model` is required" in err
    for m in ("a", "b", "c"):
        assert m in err


def test_the_error_numbers_the_choices_for_the_user(models, turns):
    models["models"] = ("a", "b")
    _, err = X._resolve_target_model(None)
    assert "1. a" in err and "2. b" in err


# --- _resolve_target_model: with a request ----------------------------------


def test_an_unconnected_model_name_is_rejected(models, turns):
    models["models"] = ("a", "b")
    name, err = X._resolve_target_model("not_connected")
    assert name == ""
    assert "is not connected" in err
    assert "1. a" in err


def test_a_connected_name_is_accepted_headless(models, turns):
    """turns is None outside Streamlit -- the MCP server and tests are not
    gated, only an interactive session is."""
    models["models"] = ("a", "b")
    turns["n"] = None
    assert X._resolve_target_model("a") == ("a", "")


def test_surrounding_whitespace_is_stripped(models, turns):
    models["models"] = ("a",)
    assert X._resolve_target_model("  a  ") == ("a", "")


@pytest.mark.parametrize("requested", ["", "   ", None])
def test_a_blank_request_is_treated_as_no_request(models, turns, requested):
    models["models"] = ("only",)
    assert X._resolve_target_model(requested) == ("only", "")


def test_a_name_is_not_validated_when_nothing_is_connected(models, turns):
    """With no connection there is no list to check against, so the name is
    taken at face value -- pinned because it is the one path where an
    unverified model name gets through."""
    models["models"] = ()
    assert X._resolve_target_model("some_model") == ("some_model", "")


# --- _model_choice_gate: consent, not inference -----------------------------


def test_one_model_never_gates(models, turns, request_state):
    models["models"] = ("only",)
    turns["n"] = 3
    assert X._model_choice_gate("only") == ""


def test_headless_never_gates(models, turns, request_state):
    models["models"] = ("a", "b")
    turns["n"] = None
    assert X._model_choice_gate("a") == ""


def test_the_first_attempt_asks_the_user(models, turns, request_state):
    models["models"] = ("kaizen_v7", "MavenMarket_Report")
    turns["n"] = 1
    msg = X._model_choice_gate("MavenMarket_Report")
    assert msg.startswith("STOP")
    assert "QUESTION: Which semantic model should I use?" in msg
    assert "1. kaizen_v7" in msg and "2. MavenMarket_Report" in msg
    assert "3. Other" in msg
    assert "MavenMarket_Report" in msg


def test_retrying_inside_the_same_turn_stays_gated(models, turns, request_state):
    """The exact failure the gate exists for: blocked once, the assistant calls
    the tool again in the same turn and treats its own retry as consent."""
    models["models"] = ("a", "b")
    turns["n"] = 1
    assert X._model_choice_gate("a").startswith("STOP")
    assert X._model_choice_gate("a").startswith("STOP")
    assert X._model_choice_gate("a").startswith("STOP")


def test_a_new_user_message_clears_the_gate(models, turns, request_state):
    models["models"] = ("a", "b")
    turns["n"] = 1
    assert X._model_choice_gate("a").startswith("STOP")
    turns["n"] = 2
    assert X._model_choice_gate("a") == ""


def test_a_new_user_message_only_clears_the_model_that_was_asked_about(
    models, turns, request_state
):
    models["models"] = ("a", "b")
    turns["n"] = 1
    X._model_choice_gate("a")
    turns["n"] = 2
    assert X._model_choice_gate("b").startswith("STOP")


def test_an_already_confirmed_model_is_not_re_gated(models, turns, request_state):
    models["models"] = ("a", "b")
    turns["n"] = 5
    request_state["_model_confirmed"] = "a"
    assert X._model_choice_gate("a") == ""


def test_switching_to_another_model_re_gates(models, turns, request_state):
    models["models"] = ("a", "b")
    turns["n"] = 5
    request_state["_model_confirmed"] = "a"
    assert X._model_choice_gate("b").startswith("STOP")


def test_confirmation_is_recorded_when_the_gate_clears(models, turns, request_state):
    models["models"] = ("a", "b")
    turns["n"] = 1
    X._resolve_target_model("a")  # gated, records the ask
    turns["n"] = 2
    name, err = X._resolve_target_model("a")
    assert (name, err) == ("a", "")
    assert request_state["_model_confirmed"] == "a"


def test_the_gate_reaches_callers_through_resolve(models, turns, request_state):
    models["models"] = ("a", "b")
    turns["n"] = 1
    name, err = X._resolve_target_model("a")
    assert name == ""
    assert err.startswith("STOP")


# --- _user_turn_count -------------------------------------------------------


def test_turn_count_is_none_when_there_is_no_message_list(request_state):
    assert X._user_turn_count() is None


def test_turn_count_counts_only_user_messages(request_state):
    request_state["messages"] = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "again"},
        "a stray string",
        {"no role": True},
    ]
    assert X._user_turn_count() == 2


def test_a_non_list_message_store_reads_as_headless(request_state):
    request_state["messages"] = "not a list"
    assert X._user_turn_count() is None


# --- _validate_bind_fields: the hallucinated-field guard --------------------


@pytest.fixture
def registry(monkeypatch):
    state = {"measures": {}, "columns": {}, "meta": {}}
    monkeypatch.setattr(X, "_load_measure_registry", lambda: state["measures"])
    monkeypatch.setattr(X, "_load_column_registry", lambda: state["columns"])
    monkeypatch.setattr(X, "_load_registry_meta", lambda: state["meta"])
    return state


def test_an_empty_registry_blocks_the_bind_and_says_how_to_fix_it(
    registry, no_hallucination_log
):
    """Binding against an empty registry is unguarded, so it is refused rather
    than allowed through 'because nothing said no'."""
    err = X._validate_bind_fields({"field": "Sales[Amount]"})
    assert err and err.startswith("ERROR")
    assert "pbi_refresh_registry" in err
    assert no_hallucination_log[0][1] == "registry_empty"


def test_a_known_column_binds(registry, no_hallucination_log):
    registry["columns"] = {"Sales.Amount": "decimal"}
    assert X._validate_bind_fields({"field": "Sales[Amount]"}) is None
    assert no_hallucination_log == []


def test_a_known_measure_binds(registry, no_hallucination_log):
    registry["measures"] = {"Sales.Total Sales": "..."}
    assert X._validate_bind_fields({"value": "Sales[Total Sales]"}) is None


def test_a_model_scoped_measure_binds_from_any_table(registry, no_hallucination_log):
    """'*.Name' is a measure with no host table pinned; a bind that names any
    table for it is still valid."""
    registry["measures"] = {"*.Total Sales": "..."}
    assert X._validate_bind_fields({"value": "AnyTable[Total Sales]"}) is None


def test_a_measure_hosted_on_another_table_still_binds(registry, no_hallucination_log):
    """Measures are model-scoped in DAX; the host table in the bind need not
    match the host table in the registry."""
    registry["measures"] = {"Internal.Total Sales": "..."}
    assert X._validate_bind_fields({"value": "Sales[Total Sales]"}) is None


def test_an_unknown_field_is_refused_with_the_offending_value(
    registry, no_hallucination_log
):
    registry["columns"] = {"Sales.Amount": "decimal"}
    err = X._validate_bind_fields({"field": "Sales[Amont]"})
    assert err and "do not exist in the connected model" in err
    assert "Sales[Amont]" in err
    assert no_hallucination_log[0][1] == "field_not_in_registry"


def test_every_bad_role_is_reported_at_once(registry, no_hallucination_log):
    """One round trip per bad field would cost a turn each; the guard names
    them all."""
    registry["columns"] = {"Sales.Amount": "decimal"}
    err = X._validate_bind_fields(
        {
            "field": "Sales[Amount]",
            "category": "Sales[Nope]",
            "value": "Sales[AlsoNope]",
        }
    )
    assert "Sales[Nope]" in err and "Sales[AlsoNope]" in err
    assert "category=" in err and "value=" in err


@pytest.mark.parametrize(
    "role",
    [
        "field",
        "category",
        "value",
        "row",
        "column",
        "line",
        "legend",
        "indicator",
        "trend",
        "goal",
        "max",
    ],
)
def test_every_bind_role_is_checked(registry, no_hallucination_log, role):
    """A role missing from the loop is an unguarded hole for that visual type."""
    registry["columns"] = {"Sales.Amount": "decimal"}
    assert X._validate_bind_fields({role: "Sales[Ghost]"}) is not None


@pytest.mark.parametrize(
    "raw",
    ["Amount", "", "Sales", "just some text"],
    ids=["bare name", "empty", "table only", "prose"],
)
def test_values_without_brackets_are_not_field_references(
    registry, no_hallucination_log, raw
):
    """Roles also carry non-field values (a title, a constant). Only
    Table[Field] shaped input is checked."""
    registry["columns"] = {"Sales.Amount": "decimal"}
    assert X._validate_bind_fields({"field": raw}) is None


@pytest.mark.parametrize(
    "raw",
    ["'Sales'[Amount]", '"Sales"[Amount]', "  Sales[Amount]", "Sales[ Amount ]"],
    ids=["single quotes", "double quotes", "leading pad", "padded field"],
)
def test_quoting_and_padding_do_not_defeat_the_lookup(
    registry, no_hallucination_log, raw
):
    registry["columns"] = {"Sales.Amount": "decimal"}
    assert X._validate_bind_fields({"field": raw}) is None


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: the field half is parsed as raw.split('[', 1)[1].rstrip(']').strip() "
        "-- rstrip runs BEFORE strip, so trailing whitespace after the closing "
        "bracket leaves the ']' attached and the key becomes 'Sales.Amount]'. A "
        "valid bind is then refused as a hallucinated field, and the error tells "
        "the model to go re-read a schema that was never wrong. Swapping to "
        ".strip().rstrip(']').strip() fixes it; delete this marker then."
    ),
)
@pytest.mark.parametrize(
    "raw",
    ["Sales[Amount] ", "  Sales  [Amount]  ", "Sales[Amount]\n"],
    ids=["trailing space", "padded both ends", "trailing newline"],
)
def test_trailing_whitespace_after_the_bracket_does_not_break_the_lookup(
    registry, no_hallucination_log, raw
):
    registry["columns"] = {"Sales.Amount": "decimal"}
    assert X._validate_bind_fields({"field": raw}) is None


def test_the_refusal_names_the_models_the_registry_covers(
    registry, no_hallucination_log
):
    """Without this the model retries the same wrong name against what it
    assumes is the right model."""
    registry["columns"] = {"Sales.Amount": "decimal"}
    registry["meta"] = {"model_names": ["kaizen_v7"], "generated_at": "2026-08-21"}
    err = X._validate_bind_fields({"field": "Sales[Ghost]"})
    assert "kaizen_v7" in err and "2026-08-21" in err


def test_field_matching_is_case_sensitive(registry, no_hallucination_log):
    """Power BI field names are case sensitive in queryRef, and the docstring's
    own gotchas ('lea_name' not 'LEA Name') depend on this."""
    registry["columns"] = {"Sales.Amount": "decimal"}
    assert X._validate_bind_fields({"field": "Sales[amount]"}) is not None
