"""Scenario matrix for the provider-agnostic half of the agent loop.

These helpers decide three things that reach the user directly: whether a turn
is allowed to be reported as clean (`_verify_agent_output`), what the model is
told when a tool blew up (`_tool_error_text`), and which events the UI renders
(`_record_tool_event`). Every one of them used to live in app.py, where nothing
could import them -- app.py runs st.set_page_config() and require_login() at
module scope -- so none of this was covered.

The verification gate matters most. It is the check standing between "the agent
narrated a successful build" and "the build actually happened", and its failure
mode is silent: a gate that returns True too easily produces a confident,
wrong answer rather than a visible error.
"""

from __future__ import annotations

import pytest

import llm.agent_runtime as AR


def _text(content: str) -> dict:
    return {"type": "text", "content": content}


def _tool(name: str, result: str = "ok, wrote 3 visuals") -> dict:
    return {"type": "tool_call", "name": name, "input": {}, "result": result}


# --- the verification gate ------------------------------------------------


def test_a_turn_with_no_events_is_never_clean():
    ok, reason = AR._verify_agent_output("build a report", [])
    assert ok is False
    assert "No events" in reason


def test_plain_text_with_no_claims_passes():
    ok, reason = AR._verify_agent_output("summarise the model", [_text("Done.")])
    assert (ok, reason) == (True, "Verified clean")


def test_pseudo_code_tool_blocks_fail_instead_of_passing_as_prose():
    evts = [_text("[tool_code] pbi_report_create(...) [/tool_code]")]
    ok, reason = AR._verify_agent_output("build it", evts)
    assert ok is False
    assert "pseudo-code" in reason


def test_a_failed_tool_call_fails_the_turn_and_names_the_tool():
    evts = [_tool("pbi_visual_add", "ERROR - could not bind field")]
    ok, reason = AR._verify_agent_output("add a chart", evts)
    assert ok is False
    assert "pbi_visual_add" in reason


def test_a_tagged_failure_is_caught_not_just_a_bare_error_prefix():
    """`[format] ERROR - ...` is the shape several report writers return."""
    evts = [_tool("pbi_visual_format", "[format] ERROR - unknown property")]
    ok, _ = AR._verify_agent_output("format it", evts)
    assert ok is False


def test_an_empty_tool_result_counts_as_a_failure():
    """Silence confirms nothing; treating it as success is the bug is_failure exists for."""
    evts = [_tool("pbi_report_create", "")]
    ok, _ = AR._verify_agent_output("build it", evts)
    assert ok is False


def test_a_skip_result_is_a_failure_because_the_work_did_not_happen():
    evts = [_tool("pbi_visual_update", "SKIP - wrong visual type")]
    ok, _ = AR._verify_agent_output("update it", evts)
    assert ok is False


@pytest.mark.parametrize(
    "prompt",
    ["show me a chart", "plot revenue", "a heatmap please", "trend by month"],
)
def test_a_prompt_asking_for_a_visual_requires_a_render_tool(prompt):
    ok, reason = AR._verify_agent_output(prompt, [_text("Here you go.")])
    assert ok is False
    assert "render_chart" in reason


def test_claiming_a_chart_in_prose_requires_a_render_tool():
    evts = [_text("Here is a chart of revenue.")]
    ok, _ = AR._verify_agent_output("summarise revenue", evts)
    assert ok is False


@pytest.mark.parametrize("tool_name", ["render_chart", "render_map"])
def test_either_render_tool_satisfies_the_visual_requirement(tool_name):
    evts = [_tool(tool_name), _text("Here is a chart.")]
    ok, reason = AR._verify_agent_output("show me a chart", evts)
    assert (ok, reason) == (True, "Verified clean")


def test_a_non_visual_prompt_needs_no_render_tool():
    ok, _ = AR._verify_agent_output("how many rows?", [_text("About 40,000.")])
    assert ok is True


# --- event recording ------------------------------------------------------


def test_a_short_result_is_recorded_whole():
    events: list[dict] = []
    returned = AR._record_tool_event(
        events, 100, "pbi_visual_list", {"a": 1}, "two visuals"
    )
    assert returned == "two visuals"
    assert events == [
        {
            "type": "tool_call",
            "name": "pbi_visual_list",
            "input": {"a": 1},
            "result": "two visuals",
        }
    ]


def test_an_over_long_result_is_truncated_and_says_so():
    events: list[dict] = []
    returned = AR._record_tool_event(events, 10, "pbi_dax_query", {}, "x" * 50)
    assert returned == "x" * 10 + "\n...[truncated]"
    assert events[0]["result"].endswith("[truncated]")


def test_creating_a_report_emits_the_active_path_before_the_tool_call():
    """Order matters: the UI reads active_report to know which report is live."""
    events: list[dict] = []
    AR._record_tool_event(
        events,
        500,
        "pbi_report_create",
        {},
        "made it\nACTIVE_REPORT_PATH=C:/r/My.Report\n",
    )
    assert [e["type"] for e in events] == ["active_report", "tool_call"]
    assert events[0]["path"] == "C:/r/My.Report"


def test_another_tool_mentioning_the_path_does_not_emit_an_active_report():
    events: list[dict] = []
    AR._record_tool_event(
        events, 500, "pbi_report_info", {}, "ACTIVE_REPORT_PATH=C:/r/X.Report"
    )
    assert [e["type"] for e in events] == ["tool_call"]


# --- prompt cache marking -------------------------------------------------


def test_marking_an_empty_thread_is_a_no_op():
    messages: list[dict] = []
    AR._mark_rolling_cache(messages)
    assert messages == []


def test_a_trailing_string_message_becomes_an_ephemeral_text_block():
    messages = [{"role": "user", "content": "hello"}]
    AR._mark_rolling_cache(messages)
    assert messages[0]["content"] == [
        {"type": "text", "text": "hello", "cache_control": {"type": "ephemeral"}}
    ]


def test_the_marker_moves_to_the_newest_message():
    """Only the last turn should carry it, or the cache never rolls forward."""
    messages = [
        {
            "role": "user",
            "content": [{"type": "text", "text": "old", "cache_control": {}}],
        },
        {"role": "user", "content": [{"type": "text", "text": "new"}]},
    ]
    AR._mark_rolling_cache(messages)
    assert "cache_control" not in messages[0]["content"][0]
    assert messages[1]["content"][0]["cache_control"] == {"type": "ephemeral"}


# --- the exhausted-retry warning ------------------------------------------


def test_nothing_unresolved_produces_no_warning():
    assert AR._unresolved_verification_event(False, 2, False, 2) is None


def test_an_unresolved_health_check_warns_and_counts_the_attempts():
    evt = AR._unresolved_verification_event(True, 3, False, 0)
    assert evt is not None
    assert "3 fix attempt(s)" in evt["content"]


def test_both_checks_unresolved_are_reported_together():
    evt = AR._unresolved_verification_event(True, 2, True, 2)
    assert " and " in evt["content"]


# --- tool error text ------------------------------------------------------


def test_the_error_text_names_the_tool_the_type_and_the_message():
    text = AR._tool_error_text("pbi_visual_add", ValueError("bad field"))
    assert "pbi_visual_add" in text
    assert "ValueError" in text
    assert "bad field" in text


# --- the read-only tool set -----------------------------------------------


def test_write_tools_are_not_in_the_read_only_set():
    """claim_guard uses this to decide whether a write was actually performed."""
    for name in ("pbi_report_create", "pbi_visual_add", "pbi_visual_delete"):
        assert name not in AR._READONLY_TOOLS
    for name in ("render_chart", "pbi_report_info", "pbi_visual_list"):
        assert name in AR._READONLY_TOOLS
