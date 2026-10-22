"""Telling a failed tool call from a successful one, and warning when none succeeded.

`claim_guard` is the last line of defence against the model narrating work it
never did: if the reply claims changes and no write tool actually succeeded, the
user gets a warning instead of a false success. That only works if "succeeded"
is judged correctly.

It was not. The check was `result.startswith(("ERROR", "DENIED", "✗"))`, and
the strings write tools really return on failure are `[format] ERROR — …`,
`[cond-color] SKIP — …`, a lowercase `Error: pbi command timed out …`, and
`Desktop Bridge unavailable: …`. None of them match, so every failure counted
as a success and the warning never fired.

The failure strings below are copied from the actual return statements in
report/visual_format.py, llm/pbi_tool_exec.py and report/dashboard_polish.py.
"""

from __future__ import annotations

import pytest

from llm.claim_guard import WARNING_ALL_FAILED, WARNING_NO_WRITES, ground_events
from util.tool_result import is_failure, is_success

# ---------------------------------------------------------------------------
# real failure strings this codebase produces
# ---------------------------------------------------------------------------

REAL_FAILURES = [
    # bracket-tagged — the shape that defeated the old startswith check
    "[format] ERROR — visual 'kpi_sales' not found under /reports/x.Report",
    "[button] ERROR — shape must be one of {'Blank'}, got 'Wibble'",
    "[card-image] ERROR — position must be one of ['Bottom'], got 'Sideways'",
    "[kpi-card] SKIP — 'bar_products' is 'clusteredBarChart', styling is cardVisual only",
    "[cond-color] SKIP — 'card_sales' is 'cardVisual'; cell coloring is tables only",
    "[color-by-cat] SKIP — 'donut_category' is 'donutChart'; fills are bar/column only",
    "[card-image] SKIP — 'combo_trend' is a combo chart; image slot is cardVisual only",
    # lowercase — startswith("ERROR") is case-sensitive
    "Error: pbi command timed out after 30s",
    # no prefix at all
    "Desktop Bridge unavailable: desktop_bridge module not installed",
    # the dispatcher's non-zero-exit message
    "ERROR — tool 'pbi_visual_delete' failed (exit 1): Access is denied. "
    "Nothing may have been written — verify with pbi_visual_list before continuing.",
    # plain forms that always worked
    "ERROR — bind blocked: no recognized binding role provided.",
    "DENIED — model edits are locked for this project",
]

REAL_SUCCESSES = [
    "Done.",
    "Deleted 3 visuals from page 'overview'. Page is now clean.",
    "[format] kpi_sales: value (cardVisual), bg=#1E3A5F",
    "[cond-color] svt_matrix: backColor on Measures_Table[Total Sales] #FFF→#000",
    "[button] nav_home (Blank, 'Home') at (8,8) 120x32",
    "[kpi-card] card_sales: accent #6366F1, value 32pt",
    "status: bound\nname: combo_trend\nbindings: [{'role': 'Category', ...}]",
    "Report reloaded in Power BI Desktop.",
]


@pytest.mark.parametrize("result", REAL_FAILURES)
def test_real_failure_strings_are_classified_as_failures(result):
    assert is_failure(result), f"classified as SUCCESS: {result!r}"


@pytest.mark.parametrize("result", REAL_SUCCESSES)
def test_real_success_strings_are_not_classified_as_failures(result):
    assert is_success(result), f"classified as FAILURE: {result!r}"


def test_an_empty_result_is_a_failure():
    """A write tool that returned nothing has confirmed nothing.

    Treating silence as success is the exact defect this module exists to
    remove, so it must not be reintroduced at the empty case.
    """
    assert is_failure("")
    assert is_failure(None)
    assert is_failure("   \n ")


def test_an_error_mentioned_late_in_a_long_success_is_not_a_failure():
    """Guard against over-matching — a false failure is the worse error.

    Reporting successful writes as failures would train the user to ignore
    the warning, which costs more than the missed detection it replaced.
    """
    msg = "Bound 12 fields successfully. " + "x" * 300 + " previous error was recovered"
    assert is_success(msg)


# ---------------------------------------------------------------------------
# the guard that consumes it
# ---------------------------------------------------------------------------


def _events(claim: str, tool_result: str) -> list[dict]:
    return [
        {"type": "text", "content": claim},
        {
            "type": "tool_call",
            "name": "pbi_visual_format",
            "input": {"visual_name": "kpi_sales", "report_path": "/r.Report"},
            "result": tool_result,
        },
    ]


def test_a_claim_backed_only_by_a_failed_write_is_warned_about():
    """The regression that mattered: a tagged ERROR read as success.

    The model says it styled the card, the only write returned
    "[format] ERROR — …", and the user was previously shown no warning at all.
    """
    out = ground_events(_events("I've styled the KPI card.", REAL_FAILURES[0]))
    assert any(e.get("content") == WARNING_ALL_FAILED for e in out)


def test_a_claim_backed_only_by_a_skip_is_warned_about():
    """SKIP means the tool declined to act — nothing was changed."""
    out = ground_events(_events("I've applied the heatmap.", REAL_FAILURES[4]))
    assert any(e.get("content") == WARNING_ALL_FAILED for e in out)


def test_a_claim_backed_by_a_real_write_is_left_alone():
    out = ground_events(_events("I've styled the KPI card.", REAL_SUCCESSES[2]))
    assert not any(
        e.get("content") in (WARNING_ALL_FAILED, WARNING_NO_WRITES) for e in out
    )


def test_a_claim_with_no_write_tool_at_all_is_warned_about():
    out = ground_events([{"type": "text", "content": "I've created the report."}])
    assert any(e.get("content") == WARNING_NO_WRITES for e in out)


def test_a_reply_that_claims_nothing_is_never_warned_about():
    """No claim, no warning — the guard must not nag on read-only turns."""
    out = ground_events(
        [{"type": "text", "content": "The report has 5 pages and 16 visuals."}]
    )
    assert len(out) == 1
