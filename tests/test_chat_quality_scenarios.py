"""Exercise the production stream-to-answer path without a provider or Fabric."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from api import chat
from llm import llm_client
from llm.answer_quality import assess_answer, checked_content
from llm.tool_evidence import ClaudeToolCollector, ToolEvidence
from util.tool_result import is_failure


def call(name="pbi_paginated_set", args=None, call_id="one"):
    return {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": call_id,
                    "name": f"mcp__pbi__{name}",
                    "input": args or {"path": "sales.rdl"},
                }
            ]
        },
    }


def result(value="Saved", call_id="one", **extra):
    return {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": value,
                    **extra,
                }
            ]
        },
    }


def stream_text(value):
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": value},
        },
    }


@pytest.fixture
def cli(monkeypatch):
    """Use the real CLI parser against a pipe containing protocol messages."""
    captured = []
    monkeypatch.setattr(llm_client, "_find_claude_bin", lambda: "claude")

    def install(messages):
        def spawn(cmd, prompt, env=None):
            captured.append({"cmd": cmd, "prompt": prompt, "env": env})
            output = "\n".join(json.dumps(message) for message in messages)
            process = SimpleNamespace(stdout=io.StringIO(output), wait=lambda: 0)
            return process, None, []

        monkeypatch.setattr(llm_client, "_spawn_cli_process", spawn)
        return captured

    return install


def test_real_cli_stream_keeps_text_usage_and_pairs_replayed_tools(cli):
    messages = [
        call(),
        call(),
        stream_text("Working."),
        result([{"type": "text", "text": "Saved"}]),
        result("Saved"),
        {"type": "result", "usage": {"input_tokens": 12, "output_tokens": 5}},
    ]
    cli(messages)
    evidence = []
    chunks = list(
        llm_client._stream_claude_cli("s", "u", 100, on_tool_event=evidence.append)
    )
    assert chunks[0] == "Working."
    assert chunks[-1].input_tokens == 12
    assert chunks[-1].output_tokens == 5
    assert len(evidence) == 1
    assert evidence[0].name == "pbi_paginated_set"
    assert evidence[0].input == {"path": "sales.rdl"}
    assert evidence[0].succeeded


def test_callbacks_are_opt_in_and_do_not_change_plain_inference(cli):
    cli([call(), result("Saved"), stream_text("Hello")])
    chunks = list(llm_client._stream_claude_cli("s", "u", 100))
    assert chunks[0] == "Hello"
    assert all(isinstance(chunk, (str, llm_client.UsageInfo)) for chunk in chunks)


@pytest.mark.parametrize("records", [[call()], [result("Saved")]])
def test_missing_half_of_exchange_never_confirms_completion(records):
    collector = ClaudeToolCollector()
    assert not [event for record in records for event in collector.feed(record)]
    remaining = collector.finish()
    assert len(remaining) == 1 and not remaining[0].succeeded
    assert collector.finish() == []


def test_results_can_arrive_before_replayed_complete_call():
    collector = ClaudeToolCollector()
    assert collector.feed(result("Saved")) == []
    assert collector.feed(call())[0].succeeded


@pytest.mark.parametrize(
    "value",
    [
        {"isError": True, "content": [{"type": "text", "text": "Looks fine"}]},
        {"success": False},
        {"ok": False},
        {"valid": False},
        {"status": "failed"},
        {"errors": ["bad binding"]},
        "INVALID:\n- missing dataset",
        [{"type": "text", "text": "Saved"}, {"type": "text", "text": "ERROR: invalid"}],
        '[{"type":"text","text":"REFUSED: no permission"}]',
    ],
)
def test_structured_failures_are_not_misread_as_success(value):
    assert is_failure(value)


@pytest.mark.parametrize(
    "value",
    [
        {"rows": []},
        {"rows": [{"error": "normal data", "success": False}]},
        [{"error": 12}],
        '[{"error": 12}]',
        {"errors": [], "valid": True},
        [{"type": "text", "text": '{"rows": [], "count": 0}'}],
    ],
)
def test_empty_data_and_error_named_columns_are_valid_query_results(value):
    assert not is_failure(value)


def test_transport_error_flag_overrides_success_looking_text():
    collector = ClaudeToolCollector()
    collector.feed(call())
    event = collector.feed(result("Saved", is_error=True))[0]
    assert not event.succeeded


@pytest.mark.parametrize("value", [None, False, [], {}, "", {"content": []}])
def test_no_result_content_is_not_confirmation(value):
    assert is_failure(value)


def test_requested_chart_cannot_be_silently_replaced_by_done():
    quality = assess_answer("Show a chart", "Done.", [], [])
    assert quality.blocking
    assert "missing_visual" in quality.issues


@pytest.mark.parametrize(
    "reply", ["Which model should I use?", "I couldn't generate the chart."]
)
def test_chart_clarification_and_honest_failure_are_kept(reply):
    quality = assess_answer("Show a chart", reply, [], [])
    assert quality.issues == []


def test_kwargs_wrapper_is_normalized_for_target_checks():
    collector = ClaudeToolCollector()
    collector.feed(call(args={"kwargs": {"path": "sales.rdl"}}))
    assert collector.feed(result())[0].input == {"path": "sales.rdl"}


@pytest.mark.parametrize("bad_id", [None, [], {}, ""])
def test_malformed_ids_do_not_break_the_chat(bad_id):
    collector = ClaudeToolCollector()
    assert collector.feed(call(call_id=bad_id)) == []
    assert collector.feed(result(call_id=bad_id)) == []
    assert collector.finish() == []


@pytest.fixture
def web_chat(monkeypatch, cli):
    monkeypatch.setattr(chat, "_caller_role", lambda: "editor")
    monkeypatch.setattr(chat, "_active_workspace", lambda: "QA")
    monkeypatch.setattr(chat, "_active_models", lambda: ["Sales"])
    monkeypatch.setattr(chat, "_project_context", lambda: "")
    monkeypatch.setattr(chat, "_drain_visuals", lambda _: [])
    monkeypatch.setattr(chat, "get_provider", lambda _: "claude_cli")
    monkeypatch.setattr(chat, "get_model_name", lambda _: "offline")
    monkeypatch.setattr(llm_client, "get_provider", lambda _: "claude_cli")
    return cli


def test_web_chat_suppresses_false_success_and_steps(web_chat):
    captured = web_chat(
        [
            call(),
            result("REFUSED: no permission"),
            stream_text("I updated the report." + chat.BLOCK_SEPARATOR + "Done."),
        ]
    )
    reply = chat.run_chat([{"role": "user", "content": "Update the report"}])
    assert reply["quality"]["status"] == "limited"
    assert "unsupported_completion" in reply["quality"]["issues"]
    assert "Done" not in reply["content"]
    assert reply["steps"] == []
    assert len(captured) == 1  # no duplicated writes or hidden repair calls
    assert "Answer quality:" in captured[0]["prompt"]
    assert "REFUSED" not in json.dumps(reply)  # no raw evidence in the response


def test_web_chat_preserves_valid_answer_and_usage(web_chat):
    web_chat(
        [
            call(
                "pbi_dax_query", {"dax": 'EVALUATE ROW("Sales", 42)', "model": "Sales"}
            ),
            result('{"rows": [{"Sales": 42}]}'),
            stream_text("Total sales: 42 USD. Source: Sales; all dates."),
            {"type": "result", "usage": {"input_tokens": 8, "output_tokens": 13}},
        ]
    )
    reply = chat.run_chat([{"role": "user", "content": "What are total sales?"}])
    assert reply["quality"] == {
        "status": "checked",
        "issues": [],
        "tool_calls": 1,
        "succeeded": 1,
    }
    assert "42 USD" in reply["content"]
    assert reply["usage"] == {"input_tokens": 8, "output_tokens": 13}


def test_followup_checks_latest_user_prompt_not_earlier_create_request(web_chat):
    web_chat([stream_text("A measure calculates a value in filter context.")])
    reply = chat.run_chat(
        [
            {"role": "user", "content": "Create a sales report"},
            {"role": "assistant", "content": "I created it."},
            {"role": "user", "content": "Explain what a measure is"},
        ]
    )
    assert reply["quality"]["issues"] == []


def test_turn_evidence_does_not_leak_between_requests(web_chat):
    web_chat(
        [call("pbi_dax_query"), result('{"rows": [42]}'), stream_text("Sales: 42.")]
    )
    assert (
        chat.run_chat([{"role": "user", "content": "Total sales?"}])["quality"][
            "issues"
        ]
        == []
    )
    web_chat([stream_text("Sales: 42.")])
    reply = chat.run_chat([{"role": "user", "content": "Total sales?"}])
    assert "missing_query_evidence" in reply["quality"]["issues"]


def test_missing_result_is_reported_even_when_provider_returns_text(web_chat):
    web_chat([call(), stream_text("Done.")])
    reply = chat.run_chat([{"role": "user", "content": "Update report"}])
    assert "unresolved_tool_failure" in reply["quality"]["issues"]


def test_other_providers_cannot_claim_actions_without_tool_evidence(
    web_chat, monkeypatch
):
    monkeypatch.setattr(
        chat, "stream_text", lambda *a, **kw: iter(["I updated the report."])
    )
    reply = chat.run_chat([{"role": "user", "content": "Update report"}])
    assert "unsupported_completion" in reply["quality"]["issues"]


def test_blocked_number_is_removed_not_left_above_disclaimer():
    quality = assess_answer("Total sales?", "Sales: 999 USD.", [], [])
    assert "999" not in checked_content("Sales: 999 USD.", quality)


@pytest.mark.parametrize("reply", ["Sales: 999 USD.", "Sales: `999` USD."])
def test_what_is_total_is_a_data_question_not_an_explanation(reply):
    quality = assess_answer("What is total sales?", reply, [], [])
    assert "missing_query_evidence" in quality.issues


def test_negative_clause_does_not_excuse_a_false_completion():
    quality = assess_answer(
        "Update report",
        "I couldn't validate it, but I updated the report.",
        [],
        [],
    )
    assert "unsupported_completion" in quality.issues


def test_deleted_file_does_not_require_reading_a_file_that_no_longer_exists():
    event = ToolEvidence("1", "pbi_paginated_delete", {"path": "sales.rdl"}, "Deleted")
    quality = assess_answer("Delete the report", "I deleted the report.", [event], [])
    assert quality.issues == []


@pytest.mark.parametrize(
    "name,flag,answer",
    [
        ("pbi_tmdl_doctor", "apply", "I fixed the model."),
        ("pbi_generate_docs", "save", "I saved the documentation."),
    ],
)
def test_preview_cannot_substantiate_an_applied_change(name, flag, answer):
    for apply in (False, True):
        event = ToolEvidence("1", name, {flag: apply}, "ok")
        quality = assess_answer("Fix and save it", answer, [event], [])
        assert ("unsupported_completion" in quality.issues) is not apply


def test_successful_export_substantiates_an_exported_artifact():
    event = ToolEvidence("1", "pbi_export_model", {}, "Exported model package")
    quality = assess_answer("Export model", "I saved the model package.", [event], [])
    assert quality.issues == []


def test_explicit_error_cannot_be_cleared_by_different_target():
    events = [
        ToolEvidence("a", "pbi_paginated_set", {"path": "a.rdl"}, "ERROR: bad XML"),
        ToolEvidence("b", "pbi_paginated_set", {"path": "b.rdl"}, "Saved"),
    ]
    quality = assess_answer(
        "Update both reports", "I updated both reports.", events, []
    )
    assert quality.blocking


def test_readback_must_follow_the_last_write_and_match_full_path():
    def event(name, path):
        return ToolEvidence(name, name, {"path": path}, "ok")

    write = event("pbi_paginated_set", "a/sales.rdl")
    right = event("pbi_paginated_get", "a/sales.rdl")
    wrong = event("pbi_paginated_get", "b/sales.rdl")
    for events in ([write, wrong], [right, write], [write, right, write]):
        assert (
            "missing_report_readback"
            in assess_answer("Update", "Saved.", events, []).issues
        )
    assert not assess_answer("Update", "Saved.", [write, right], []).issues


def test_failed_validator_cannot_leave_done_above_a_disclaimer():
    events = [
        ToolEvidence("1", "pbi_paginated_set", {"path": "sales.rdl"}, "Saved"),
        ToolEvidence(
            "2", "pbi_paginated_validate", {"path": "sales.rdl"}, "INVALID: bad field"
        ),
    ]
    quality = assess_answer("Update and validate report", "Done.", events, [])
    assert quality.blocking
    assert "Done" not in checked_content("Done.", quality)


def test_validation_claim_requires_validation_after_last_change():
    write = ToolEvidence("1", "pbi_paginated_set", {"path": "sales.rdl"}, "Saved")
    validate = ToolEvidence("2", "pbi_paginated_validate", {"path": "sales.rdl"}, "ok")
    wrong = ToolEvidence("3", "pbi_paginated_validate", {"path": "other.rdl"}, "ok")
    for events in ([write], [validate, write], [write, wrong]):
        quality = assess_answer(
            "Validate report", "The report is structurally valid.", events, []
        )
        assert "unsupported_validation" in quality.issues
    quality = assess_answer(
        "Validate report", "The report is structurally valid.", [write, validate], []
    )
    assert quality.issues == []


def test_failed_query_clause_does_not_excuse_an_invented_number():
    quality = assess_answer(
        "Total sales?", "The query failed, but total sales are 42 USD.", [], []
    )
    assert quality.blocking
    assert "missing_query_evidence" in quality.issues


def test_benchmark_replays_quality_cases_without_calling_a_model():
    from evals.chat_quality import run_benchmark

    report = run_benchmark()
    assert report["mode"] == "offline_replay"
    assert report["passed"] == report["cases"] >= 30
    # Deliberate incorrect answers demonstrate the limit of evidence presence
    # checks. The independent reference grader must still catch them.
    results = {result["id"]: result for result in report["results"]}
    for key in ("wrong_arithmetic", "no_rows_is_not_zero"):
        assert results[key]["reference_passed"] is False
        assert results[key]["issues"] == []


def test_benchmark_never_fills_missing_responses_with_reference_answers():
    from evals.chat_quality import run_benchmark

    with pytest.raises(ValueError, match="every benchmark case"):
        run_benchmark({})


@pytest.mark.parametrize(
    "records",
    [
        [],
        [{"id": "wrong"}],
        [{"id": "a", "content": "ok"}] * 2,
        [{"id": "a", "content": 1}],
    ],
)
def test_recorded_benchmark_rejects_missing_duplicate_or_invalid_answers(
    tmp_path, records
):
    from evals.chat_quality import load_responses

    path = tmp_path / "responses.jsonl"
    path.write_text(
        "\n".join(json.dumps(record) for record in records), encoding="utf-8"
    )
    with pytest.raises(ValueError):
        load_responses(path, {"a"})


_TIMEOUT_EXPLANATION = """I've hit a backend problem. Here's where things stand:

- **Model:** connected and verified (Sales QA: 6 tables, 75 measures).
- **Report scaffold:** the creation call **timed out** after 30 seconds.
- **Verified:** the new report is **not** in the report index.
- **Uncertain:** the follow-up inspection also timed out, so I can't confirm
  whether a partial folder was left behind.
- **Untouched:** no visuals created.

The report backend needs to respond before I can finish the report.
""".strip()


def test_reported_timeout_explanation_survives_the_real_web_chat_path(web_chat):
    web_chat(
        [
            call("pbi_model_inspect", {"model": "Sales QA"}, "model"),
            result('{"tables": [], "measure_count": 75}', "model"),
            call(
                "pbi_report_create", {"path": "qa/report", "name": "Overview"}, "create"
            ),
            result("Error: pbi command timed out after 30s", "create"),
            call(
                "pbi_report_info", {"report_path": "qa/report/Overview.Report"}, "info"
            ),
            result("Error: pbi command timed out after 30s", "info"),
            stream_text(_TIMEOUT_EXPLANATION),
        ]
    )
    reply = chat.run_chat(
        [{"role": "user", "content": "Create a fresh single-page report"}]
    )
    assert reply["content"] == _TIMEOUT_EXPLANATION
    assert reply["quality"]["issues"] == ["unresolved_tool_failure"]


@pytest.mark.parametrize(
    "answer",
    [
        "Model connected and verified. The report creation timed out.",
        "Verified: the report exists in the report index.",
        "The report has not been validated.",
        "The report is not yet structurally valid.",
        "Model validation passed. Report creation timed out.",
    ],
)
def test_connection_and_existence_checks_are_not_structural_validation(answer):
    quality = assess_answer("Create a fresh report", answer, [], [])
    assert "unsupported_validation" not in quality.issues


@pytest.mark.parametrize(
    "answer",
    [
        "The report is structurally valid.",
        "I validated the report.",
        "Report validation passed.",
        "Verified the report structure.",
        "All checks passed.",
    ],
)
def test_actual_report_validation_claims_still_require_a_pass(answer):
    quality = assess_answer("Validate the report", answer, [], [])
    assert "unsupported_validation" in quality.issues
    assert quality.blocking


def test_real_failures_produce_specific_diagnostics_without_echoing_payloads():
    events = [
        ToolEvidence(
            "1", "pbi_report_create", {}, "Error: timed out; token=secret-value"
        ),
        ToolEvidence("2", "pbi_report_info", {}, "Error: timed out after 30s"),
    ]
    quality = assess_answer("Create report", "Done.", events, [])
    output = checked_content("Done.", quality)
    assert "Report creation timed out" in output
    assert "Report inspection timed out" in output
    assert "partial" in output
    assert "secret-value" not in output
    assert "Done" not in output


def test_schema_counts_from_model_inspection_do_not_require_a_dax_query():
    event = ToolEvidence(
        "1", "pbi_model_inspect", {}, {"table_count": 6, "measure_count": 75}
    )
    quality = assess_answer(
        "Create a dashboard using Sales QA",
        "The model has 6 tables and 75 measures. I can use `Forecast 2025`.",
        [event],
        [],
    )
    assert "missing_query_evidence" not in quality.issues


@pytest.mark.parametrize(
    "answer",
    [
        "There are 6 tables. Sales: 42 USD.",
        "The 6 tables have revenue of $42.",
        "Across 6 tables revenue is 42 USD.",
    ],
)
def test_model_metadata_does_not_support_business_figures(answer):
    event = ToolEvidence("1", "pbi_model_inspect", {}, {"table_count": 6})
    quality = assess_answer("What are total sales?", answer, [event], [])
    assert "missing_query_evidence" in quality.issues


def test_model_inventory_and_layout_choices_are_not_live_metric_values():
    event = ToolEvidence(
        "1", "pbi_model_inspect", {}, {"tables": ["Sales"], "measure_count": 75}
    )
    answer = """The model has 6 tables and 75 measures.
- Time intelligence: Rolling 3M / 12M and Forecast 2025.
Choose a layout:
1. Executive overview: one page with 6 visuals.
2. Trends: 3 pages using the approved measures.
"""
    quality = assess_answer("Create a dashboard using Sales QA", answer, [event], [])
    assert "missing_query_evidence" not in quality.issues
