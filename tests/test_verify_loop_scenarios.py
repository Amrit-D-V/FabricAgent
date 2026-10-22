"""Scenario matrix for the verify/repair loop.

scripts/test_verify_loop.py already walks this module once per function --
one placeholder, one probe failure, one repair round. That proves the parts
work; it does not prove the loop TERMINATES from every state it can reach, and
a loop that does not terminate here burns an LLM call per round against a live
warehouse.

So this covers the decision space instead: every exit reason, the round cap,
the boundary of the coverage threshold, issue de-duplication, and the DAX
reference parser's tail cases.
"""

from __future__ import annotations

import json

import pytest

from semantic_model_v2 import verify_loop as VL
from semantic_model_v2.plan import ColumnSpec, MeasureSpec, SchemaPlan, TableSpec

SCHEMA_TABLES = {
    "dbo.factpolicy": [("writtenPremium", "decimal"), ("policyKey", "int64")]
}
GOOD_DAX = "SUM('factpolicy'[writtenPremium])"


def _plan(n_measures=1, dax="BLANK()", names=None):
    if names is None:
        names = [f"Measure {i}" for i in range(1, n_measures + 1)]
    return SchemaPlan(
        model_name="M",
        server_fqdn="s",
        db_name="d",
        tables=[
            TableSpec(
                name="factpolicy",
                kind="fact",
                schema="dbo",
                source_table="factpolicy",
                columns=[
                    ColumnSpec(
                        name="writtenPremium",
                        source_column="writtenPremium",
                        data_type="decimal",
                    ),
                    ColumnSpec(
                        name="policyKey", source_column="policyKey", data_type="int64"
                    ),
                ],
            )
        ],
        measures=[
            MeasureSpec(
                name=nm,
                host_table="factpolicy",
                intent="total written premium",
                dax=dax,
            )
            for nm in names
        ],
    )


@pytest.fixture
def repairer(monkeypatch):
    """Drive the repair agent's answer per scenario, without an LLM."""
    state = {"raw": json.dumps({"reason": "r", "operations": []}), "prompts": []}

    def fake_stream_collect(system, user, **kw):
        state["prompts"].append(user)
        return state["raw"], None

    monkeypatch.setattr(VL, "stream_collect", fake_stream_collect)
    return state


def _ops_for(names):
    return json.dumps(
        {
            "reason": "intent too vague",
            "operations": [
                {
                    "kind": "update_measure",
                    "name": nm,
                    "new_intent": f"SUM of 'factpolicy'[writtenPremium] for {nm}",
                }
                for nm in names
            ],
        }
    )


def _fills(count):
    """A DAX generator that repairs exactly `count` blank measures per call."""

    def populate(plan, profile=None, project_id=None, progress_callback=None):
        left = count
        for m in plan.measures:
            if left <= 0:
                break
            if (m.dax or "").strip().upper() in ("", "BLANK()"):
                m.dax = GOOD_DAX
                left -= 1
        return plan, []

    return populate


# --- Every way the loop can exit --------------------------------------------


def test_a_clean_plan_never_calls_the_repair_agent(repairer):
    res = VL.run_verify_repair_loop(
        _plan(dax=GOOD_DAX), schema_tables=SCHEMA_TABLES, log=lambda m: None
    )
    assert res.rounds_run == 0
    assert "clean" in res.stopped_reason
    assert repairer["prompts"] == []


@pytest.fixture
def steadily_improving(monkeypatch):
    """A verifier whose error count strictly decreases each round.

    Driving this through real placeholder repair is not possible: update_measure
    clears the measure's DAX by design (see the test below), so re-patching a
    measure that was already fixed puts it back to blank and the loop correctly
    reports no progress. Injecting the sequence isolates the loop's own
    termination logic, which is what the cap is about.
    """

    def install(start):
        state = {"n": start}

        def fake(plan):
            issues = [
                VL.VerifyIssue("error", "placeholder", f"M{i}", "still blank")
                for i in range(state["n"])
            ]
            state["n"] = max(state["n"] - 1, 0)
            return issues

        monkeypatch.setattr(VL, "collect_placeholder_issues", fake)
        return state

    return install


def test_the_round_cap_is_honoured_even_while_making_progress(
    repairer, steadily_improving
):
    """Progress alone must not license an unbounded number of LLM calls."""
    steadily_improving(20)
    repairer["raw"] = _ops_for(["Measure 1"])
    res = VL.run_verify_repair_loop(
        _plan(),
        schema_tables=SCHEMA_TABLES,
        populate_measures_fn=_fills(1),
        max_rounds=2,
        log=lambda m: None,
    )
    assert res.rounds_run == 2
    assert "2-round safety cap" in res.stopped_reason
    assert res.final_errors, "unresolved errors must still be reported"


def test_the_default_cap_is_the_hard_cap(repairer, steadily_improving):
    steadily_improving(20)
    repairer["raw"] = _ops_for(["Measure 1"])
    res = VL.run_verify_repair_loop(
        _plan(),
        schema_tables=SCHEMA_TABLES,
        populate_measures_fn=_fills(1),
        log=lambda m: None,
    )
    assert res.rounds_run == VL.MAX_ROUNDS_HARD_CAP


def test_re_patching_a_fixed_measure_undoes_the_fix(repairer):
    """update_measure with a new intent clears the DAX so it regenerates. That
    means a repairer which re-sends the whole measure list every round wipes
    what the previous round fixed, and the loop stalls at 'no progress' rather
    than converging. Pinned because it is invisible from the patch alone."""
    names = ["M1", "M2", "M3"]
    repairer["raw"] = _ops_for(names)
    res = VL.run_verify_repair_loop(
        _plan(names=names),
        schema_tables=SCHEMA_TABLES,
        populate_measures_fn=_fills(1),
        log=lambda m: None,
    )
    assert "no progress" in res.stopped_reason


def test_a_round_that_fixes_nothing_stops_the_loop(repairer):
    """Same error count twice means the repairer is looping; stop rather than
    spend the remaining rounds."""
    repairer["raw"] = _ops_for(["M1", "M2"])
    res = VL.run_verify_repair_loop(
        _plan(names=["M1", "M2"]),
        schema_tables=SCHEMA_TABLES,
        populate_measures_fn=_fills(0),
        log=lambda m: None,
    )
    assert "no progress" in res.stopped_reason
    assert res.rounds_run < VL.MAX_ROUNDS_HARD_CAP


def test_an_empty_operation_list_stops_immediately(repairer):
    repairer["raw"] = json.dumps({"reason": "cannot fix", "operations": []})
    res = VL.run_verify_repair_loop(
        _plan(), schema_tables=SCHEMA_TABLES, log=lambda m: None
    )
    assert "no changes" in res.stopped_reason
    assert "cannot fix" in res.stopped_reason
    assert res.rounds_run == 0


def test_operations_that_all_get_rejected_stop_the_loop(repairer):
    """A patch naming measures that do not exist applies nothing. Continuing
    would re-ask the same question forever."""
    repairer["raw"] = _ops_for(["NoSuchMeasure"])
    res = VL.run_verify_repair_loop(
        _plan(names=["M1"]), schema_tables=SCHEMA_TABLES, log=lambda m: None
    )
    assert res.rounds_run == 0
    assert (
        "rejected as invalid" in res.stopped_reason
        or "no progress" in res.stopped_reason
    )


@pytest.mark.parametrize(
    "raw",
    ["I cannot help with that", "", "```json\n{oops}\n```", "null", "[]"],
    ids=["prose", "empty", "malformed", "null", "array"],
)
def test_an_unparseable_repair_response_stops_without_a_round(repairer, raw):
    """The repairer is an LLM; every one of these is a response it can produce,
    and none may take down the build."""
    repairer["raw"] = raw
    res = VL.run_verify_repair_loop(
        _plan(), schema_tables=SCHEMA_TABLES, log=lambda m: None
    )
    assert res.rounds_run == 0
    assert res.final_errors, "the original issues must still be reported"


def test_the_returned_plan_is_the_repaired_one(repairer):
    repairer["raw"] = _ops_for(["M1"])
    res = VL.run_verify_repair_loop(
        _plan(names=["M1"]),
        schema_tables=SCHEMA_TABLES,
        populate_measures_fn=_fills(1),
        log=lambda m: None,
    )
    assert res.plan.measures[0].dax == GOOD_DAX
    assert res.actions


def test_a_populate_function_is_optional(repairer):
    """Callers without a DAX generator still get a terminating loop."""
    repairer["raw"] = _ops_for(["M1"])
    res = VL.run_verify_repair_loop(
        _plan(names=["M1"]), schema_tables=SCHEMA_TABLES, log=lambda m: None
    )
    assert res.stopped_reason


def test_the_repair_prompt_carries_the_failures(repairer):
    repairer["raw"] = json.dumps({"operations": []})
    VL.run_verify_repair_loop(_plan(), schema_tables=SCHEMA_TABLES, log=lambda m: None)
    assert "Verification failures" in repairer["prompts"][0]


# --- Placeholder detection --------------------------------------------------


@pytest.mark.parametrize(
    "dax",
    ["", "   ", "BLANK()", "blank()", "  Blank()  ", "\n BLANK() \n"],
    ids=["empty", "whitespace", "upper", "lower", "padded", "newlines"],
)
def test_every_spelling_of_a_placeholder_is_caught(dax):
    issues = VL.collect_placeholder_issues(_plan(dax=dax))
    assert len(issues) == 1
    assert issues[0].category == "placeholder"
    assert issues[0].severity == "error"


@pytest.mark.parametrize(
    "dax",
    [GOOD_DAX, "IF(TRUE(), BLANK(), 1)", "BLANK ()", "0"],
    ids=["real measure", "blank inside a branch", "space before parens", "zero"],
)
def test_real_dax_is_not_mistaken_for_a_placeholder(dax):
    assert VL.collect_placeholder_issues(_plan(dax=dax)) == []


def test_each_blank_measure_is_reported_separately():
    plan = _plan(names=["A", "B", "C"])
    plan.measures[1].dax = GOOD_DAX
    issues = VL.collect_placeholder_issues(plan)
    assert {i.target for i in issues} == {"A", "C"}


def test_the_placeholder_message_carries_the_intent():
    """The repair agent needs the intent to regenerate; without it the round
    is wasted."""
    plan = _plan()
    plan.measures[0].intent = "total written premium net of reinsurance"
    msg = VL.collect_placeholder_issues(plan)[0].message
    assert "net of reinsurance" in msg


def test_a_plan_with_no_measures_yields_no_placeholder_issues():
    plan = _plan(names=[])
    assert VL.collect_placeholder_issues(plan) == []


# --- Coverage against the spec ----------------------------------------------

KPI_BLOCK_3 = "- [1] Premium · Gross Written Premium ($)\n- [2] Premium · Net Earned Premium ($)\n- [3] Loss · Loss Ratio (%)"


def test_no_spec_means_no_coverage_opinion():
    issues, summary = VL.collect_coverage_issues(_plan(), "")
    assert issues == [] and summary == ""


def test_a_fully_covered_spec_reports_no_gaps():
    plan = _plan(names=["Gross Written Premium"])
    issues, summary = VL.collect_coverage_issues(
        plan, "- [1] Premium · Gross Written Premium ($)"
    )
    assert issues == []
    assert "matched=1" in summary


def test_gaps_below_the_threshold_become_repairable_issues():
    plan = _plan(names=["Gross Written Premium"])
    issues, summary = VL.collect_coverage_issues(plan, KPI_BLOCK_3)
    assert {i.target for i in issues} == {
        "Premium Net Earned Premium",
        "Loss Loss Ratio",
    }
    assert all(i.category == "coverage" for i in issues)
    assert "of 3 spec KPIs" in summary


def test_coverage_at_or_above_the_threshold_reports_the_gap_without_blocking():
    """2 of 3 is 67%, over the 60% bar: the summary still names the shortfall
    but the build is not held up for it."""
    plan = _plan(names=["Gross Written Premium", "Net Earned Premium"])
    issues, summary = VL.collect_coverage_issues(plan, KPI_BLOCK_3)
    assert issues == []
    assert "missing=1" in summary


def test_a_blocked_kpi_is_excluded_from_the_denominator():
    """A KPI the source data cannot support must not drag coverage under the
    bar forever."""
    plan = _plan(names=["Gross Written Premium"])
    plan.notes = ["BLOCKED: [3] Loss Ratio — no loss table in source"]
    _issues, summary = VL.collect_coverage_issues(plan, KPI_BLOCK_3)
    assert "blocked=1" in summary


def test_duplicate_spec_lines_are_counted_once():
    plan = _plan(names=["Gross Written Premium"])
    block = "- [1] Premium · Gross Written Premium ($)\n- [2] Premium · Gross Written Premium ($)"
    _, summary = VL.collect_coverage_issues(plan, block)
    assert "of 1 spec KPIs" in summary


def test_lines_that_are_not_kpis_are_ignored():
    plan = _plan(names=["Gross Written Premium"])
    block = "Some heading\n\n- [1] Premium · Gross Written Premium ($)\n  indented note"
    _, summary = VL.collect_coverage_issues(plan, block)
    assert "of 1 spec KPIs" in summary


def test_a_near_miss_name_still_matches():
    """Matching is token overlap, not equality -- the generator is not expected
    to reproduce the spec's wording exactly."""
    plan = _plan(names=["Gross Written Premium Amount"])
    issues, _ = VL.collect_coverage_issues(
        plan, "- [1] Premium · Gross Written Premium ($)"
    )
    assert issues == []


def test_an_unrelated_measure_does_not_count_as_coverage():
    plan = _plan(names=["Headcount"])
    issues, _ = VL.collect_coverage_issues(
        plan, "- [1] Premium · Gross Written Premium ($)"
    )
    assert len(issues) == 1


# --- DAX reference extraction -----------------------------------------------


@pytest.mark.parametrize(
    "dax,expected",
    [
        ("SUM(factpolicy[writtenPremium])", {("factpolicy", "writtenPremium")}),
        ("SUM('My Table'[Col A])", {("My Table", "Col A")}),
        (
            "CALCULATE(SUM('a'[b]), c[d] > 0)",
            {("a", "b"), ("c", "d")},
        ),
        ("SUM( factpolicy [x] )", {("factpolicy", "x")}),
    ],
    ids=["bare", "quoted with spaces", "mixed", "whitespace"],
)
def test_column_references_are_extracted(dax, expected):
    assert VL._measure_column_refs(dax) == expected


@pytest.mark.parametrize(
    "dax",
    ["[JustAMeasure]", "DIVIDE([A], [B])", "", "   ", "1 + 1"],
    ids=["measure", "two measures", "empty", "whitespace", "constant"],
)
def test_measure_references_are_not_mistaken_for_columns(dax):
    """A measure reference has no table prefix. Treating it as a column sends
    the SQL probe hunting for a column that was never claimed to exist."""
    assert VL._measure_column_refs(dax) == set()


def test_none_dax_does_not_raise():
    assert VL._measure_column_refs(None) == set()


def test_the_same_reference_twice_is_one_reference():
    assert VL._measure_column_refs("f[x] + f[x]") == {("f", "x")}


# --- Issue identity ---------------------------------------------------------


def test_identical_issues_share_a_key():
    a = VL.VerifyIssue("error", "placeholder", "M", "same message")
    b = VL.VerifyIssue("error", "placeholder", "M", "same message")
    assert a.key() == b.key()


def test_target_casing_does_not_split_an_issue():
    a = VL.VerifyIssue("error", "placeholder", "Total Sales", "m")
    b = VL.VerifyIssue("error", "placeholder", "total sales", "m")
    assert a.key() == b.key()


def test_a_different_category_is_a_different_issue():
    a = VL.VerifyIssue("error", "placeholder", "M", "m")
    b = VL.VerifyIssue("error", "coverage", "M", "m")
    assert a.key() != b.key()


def test_messages_are_compared_on_their_first_120_characters():
    """Long probe messages differ only in a row count; keying on the whole
    string would report the same problem repeatedly."""
    head = "x" * 120
    a = VL.VerifyIssue("error", "live_sql", "M", head + "rows=1")
    b = VL.VerifyIssue("error", "live_sql", "M", head + "rows=2")
    assert a.key() == b.key()


# --- Generator percentage scaling and disconnected dimension tests ----------


def test_fix_percentage_dax_scaling_removes_double_100_multiplier():
    from semantic_model_v2.plan_fixups import fix_percentage_dax_scaling

    plan = _plan(
        n_measures=1,
        dax="DIVIDE([Meeting], [Total]) * 100",
        names=["Avg % Meeting Expectations"],
    )
    plan.measures[0].format_string = "0.0%"
    actions = fix_percentage_dax_scaling(plan)
    assert len(actions) == 1
    assert "* 100" not in plan.measures[0].dax
    assert "DIVIDE([Meeting], [Total])" in plan.measures[0].dax


def test_infer_missing_format_strings_assigns_percent_to_percentage_names():
    from semantic_model_v2.plan_fixups import infer_missing_format_strings

    plan = _plan(
        n_measures=3,
        dax="AVERAGE('factpolicy'[writtenPremium])",
        names=["Avg % Meeting", "Avg % Approaching", "Average Premium"],
    )
    plan.measures[0].format_string = None
    plan.measures[1].format_string = "#,0.00"  # accidentally non-percent
    plan.measures[2].format_string = None
    infer_missing_format_strings(plan)
    assert plan.measures[0].format_string == "0.0%"
    assert plan.measures[1].format_string == "0.0%"
    assert plan.measures[2].format_string == "#,0.00"


def test_validate_plan_flags_disconnected_orphan_dimension():
    from semantic_model_v2.validate import validate_plan

    plan = _plan(n_measures=1, dax=GOOD_DAX, names=["Total Written"])
    # Add an orphan dimension table with 0 relationships
    plan.tables.append(
        TableSpec(
            name="Student Group",
            kind="dim",
            schema="dbo",
            source_table="dim_student_group",
            columns=[
                ColumnSpec(
                    name="group_id",
                    source_column="group_id",
                    data_type="int64",
                    is_key=True,
                ),
                ColumnSpec(
                    name="group_name", source_column="group_name", data_type="string"
                ),
            ],
        )
    )
    schema_tables = {
        "dbo.factpolicy": [("writtenPremium", "decimal"), ("policyKey", "int64")],
        "dbo.dim_student_group": [("group_id", "int64"), ("group_name", "varchar")],
    }
    errors = validate_plan(plan, schema_tables)
    dim_warnings = [e for e in errors if "disconnected" in e.message.lower()]
    assert len(dim_warnings) == 1
    assert "Student Group" in dim_warnings[0].message
