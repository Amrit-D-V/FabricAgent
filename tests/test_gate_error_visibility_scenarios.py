"""Scenario matrix for what the validation gates do when a check BREAKS.

Every gate in this repo has two ways to answer "no problems found": it looked
and there was nothing, or it never looked. Those read identically to a caller,
and the second one is the expensive mistake — FabricBI's whole claim is that the
model it hands back is correct, so a swallowed exception inside a check turns
"this model is WRONG" into "this looks fine" and ships it.

So none of these test whether a rule is right. They test the one property that
sits underneath all of them: a check that failed to run is never reported as a
check that passed. The clean-run cases are here for the same reason half of
test_bpa_engine_scenarios.py is false-positive tests — a gate that screams
"incomplete" on a healthy run is just as useless.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from model import bpa_engine, validation_core
from model.bpa_engine import RULES, evaluate
from model.validation_core import ExecutionResult, _exec_error, compare_results
from report import bpa_lint, rdl_builder
from semantic_model_v2 import validate as sm_validate
from semantic_model_v2.plan import ColumnSpec, MeasureSpec, SchemaPlan, TableSpec

# --- model.bpa_engine: a rule that throws ------------------------------------


def _meta(measures=(), columns=(), relationships=(), tables=()):
    return {
        "measures": list(measures),
        "columns": list(columns),
        "relationships": list(relationships),
        "tables": list(tables),
    }


@pytest.fixture
def one_broken_rule(monkeypatch):
    """Make the first rule blow up, leaving the other eleven working."""

    def boom(meta):
        raise RuntimeError("rule internals exploded")

    rule_id, name, severity, _fn = RULES[0]
    patched = [(rule_id, name, severity, boom)] + list(RULES[1:])
    monkeypatch.setattr(bpa_engine, "RULES", patched)
    return rule_id


def test_a_rule_that_throws_is_not_counted_as_a_rule_that_passed(one_broken_rule):
    report = evaluate(_meta())
    assert report["rules_errored"]
    assert report["rules_errored"][0]["rule"] == one_broken_rule
    assert report["complete"] is False


def test_a_run_with_a_broken_rule_does_not_claim_every_rule_ran(one_broken_rule):
    """The summary is what a human reads. '12 of 12' over an 11-rule run is the
    sentence that gets a broken model signed off."""
    report = evaluate(_meta())
    assert f"{len(RULES)} of {len(RULES)}" not in report["summary"]
    assert f"{len(RULES) - 1} of {len(RULES)}" in report["summary"]
    assert "FAILED TO RUN" in report["summary"]


def test_a_broken_rule_still_leaves_the_other_rules_reporting(one_broken_rule):
    """Failing loudly must not mean failing shut — the eleven rules that work
    are the reason anyone runs this at all."""
    report = evaluate(
        _meta(measures=[{"table": "Sales", "name": "M", "expression": "[a] / [b]"}])
    )
    fired = " ".join(f["rule"] for f in report["warnings"])
    assert "DIVIDE" in fired


def test_a_healthy_run_is_marked_complete():
    report = evaluate(
        _meta(
            measures=[
                {
                    "table": "Sales",
                    "name": "M",
                    "expression": "SUM(Sales[Amount])",
                    "format_string": "#,0",
                    "display_folder": "Core",
                    "is_hidden": False,
                    "description": "a measure",
                }
            ],
            tables=[{"name": "Sales", "is_hidden": False}],
        )
    )
    assert report["complete"] is True
    assert report["rules_errored"] == []
    assert "FAILED TO RUN" not in report["summary"]


def test_degenerate_metadata_is_reported_as_unchecked_not_as_clean():
    """`{"measures": None}` breaks most rules. It used to come back as twelve
    rules run and nothing found."""
    report = evaluate({"measures": None})
    assert report["rules_errored"]
    assert report["complete"] is False


# --- report.bpa_lint: inspector output that cannot be read -------------------


@pytest.fixture
def inspector(monkeypatch, tmp_path):
    """Pretend PBI-Inspector is installed and drive what it leaves behind."""
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    monkeypatch.setattr(bpa_lint, "inspector_available", lambda: True)

    state: dict[str, list] = {"files": []}

    class _Proc:
        stdout = ""
        stderr = ""
        returncode = 0

    def fake_run(cmd, **kw):
        out_dir = Path(cmd[cmd.index("-output") + 1])
        for i, text in enumerate(state["files"]):
            (out_dir / f"result{i}.json").write_text(text, encoding="utf-8")
        return _Proc()

    monkeypatch.setattr(bpa_lint.subprocess, "run", fake_run)
    state["path"] = str(report_dir)
    return state


def _passing_result(rule="R1"):
    return json.dumps([{"RuleName": rule, "Pass": True, "LogType": 2}])


def _failing_result(rule="R1"):
    return json.dumps([{"RuleName": rule, "Pass": False, "LogType": 2}])


def test_unreadable_inspector_output_is_not_zero_errors_zero_warnings(inspector):
    """The failure this whole file exists for: the lint did not happen and the
    gate said the report was clean."""
    inspector["files"] = ["{ this is not json"]
    result = bpa_lint.lint_report(inspector["path"])
    assert result.get("error")
    assert "0 errors · 0 warnings" not in result.get("summary", "")


def test_unreadable_inspector_output_names_the_file_and_the_reason(inspector):
    inspector["files"] = ["{ this is not json"]
    result = bpa_lint.lint_report(inspector["path"])
    assert len(result["unreadable_results"]) == 1
    assert "result0.json" in result["unreadable_results"][0]


def test_a_partly_unreadable_lint_is_reported_as_incomplete(inspector):
    """One good file among bad ones is the nastiest case: there are real
    findings to show, so the result looks authoritative."""
    inspector["files"] = [_failing_result("Rule A"), "{ truncated"]
    result = bpa_lint.lint_report(inspector["path"])
    assert result["errors"]
    assert "INCOMPLETE" in result["summary"]
    assert result["unreadable_results"]


def test_a_fully_readable_lint_is_not_marked_incomplete(inspector):
    inspector["files"] = [_passing_result()]
    result = bpa_lint.lint_report(inspector["path"])
    assert "error" not in result
    assert "INCOMPLETE" not in result["summary"]
    assert "unreadable_results" not in result


def test_an_inspector_that_produces_nothing_is_an_error_not_a_pass(inspector):
    inspector["files"] = []
    result = bpa_lint.lint_report(inspector["path"])
    assert result.get("error")


# --- report.bpa_lint: rule severities that cannot be read -------------------


@pytest.fixture
def te2(monkeypatch, tmp_path):
    """Drive the TE2 path with one violation and a controllable rules file."""
    monkeypatch.setattr(bpa_lint, "te2_available", lambda: True)

    class _Proc:
        stdout = "'Sales'[M] violates rule \"Do not use floats\"\n"
        stderr = ""
        returncode = 0

    monkeypatch.setattr(bpa_lint.subprocess, "run", lambda cmd, **kw: _Proc())
    return tmp_path


def test_missing_rule_severities_do_not_pass_as_the_real_severities(te2, monkeypatch):
    """Unmapped rules default to 'warning'. Losing the rules file therefore
    demotes every one of Microsoft's error rules, and the caller is entitled to
    know the severities it is reading are fabricated."""
    monkeypatch.setattr(bpa_lint, "BPA_RULES_PATH", te2 / "does-not-exist.json")
    result = bpa_lint.lint_model("Model", server="fake-server")
    assert result.get("severity_error")
    assert "SEVERITIES UNKNOWN" in result["summary"]


def test_readable_rule_severities_leave_no_warning_on_the_result(te2, monkeypatch):
    rules = te2 / "rules.json"
    rules.write_text(
        json.dumps([{"Name": "Do not use floats", "Severity": 3}]), encoding="utf-8"
    )
    monkeypatch.setattr(bpa_lint, "BPA_RULES_PATH", rules)
    result = bpa_lint.lint_model("Model", server="fake-server")
    assert "severity_error" not in result
    assert "SEVERITIES UNKNOWN" not in result["summary"]
    assert result["errors"]


# --- model.validation_core: a query that never executed ---------------------


@pytest.mark.parametrize(
    "exc",
    [ValueError(), RuntimeError(""), RuntimeError("   ")],
    ids=["no args", "empty message", "whitespace message"],
)
def test_an_exception_with_no_message_still_produces_a_visible_error(exc):
    """compare_results tests `.error` for truth, so a blank message is the same
    as no error at all."""
    assert _exec_error(exc)


def test_a_query_that_raised_blankly_is_not_compared_as_a_match():
    """Both sides return zero rows because neither side ran. Reading that as
    'match' certifies a visual nobody checked."""
    dax = ExecutionResult(rows=[], row_count=0, error=_exec_error(ValueError()))
    sql = ExecutionResult(rows=[], row_count=0)
    assert compare_results(dax, sql).status == "error"


def test_a_sql_side_that_raised_blankly_is_not_compared_as_a_match():
    dax = ExecutionResult(rows=[], row_count=0)
    sql = ExecutionResult(rows=[], row_count=0, error=_exec_error(ValueError()))
    assert compare_results(dax, sql).status == "error"


def test_the_error_text_names_the_exception_type():
    """`str(exc)` alone on a connection failure reads as prose with no clue
    what threw."""
    assert "TimeoutError" in _exec_error(TimeoutError("took too long"))


def test_two_genuinely_empty_result_sets_still_match():
    """The control. A query that ran and found nothing is a real match, and
    turning that into an error would fail every legitimately empty visual."""
    dax = ExecutionResult(rows=[], row_count=0)
    sql = ExecutionResult(rows=[], row_count=0)
    assert compare_results(dax, sql).status == "match"


def test_a_visual_with_an_unreadable_title_is_still_validated(tmp_path):
    """Title extraction is cosmetic and may fail on a hand-edited visual.json.
    The bindings are the point, and they must survive it."""
    pages = tmp_path / "R.Report" / "definition" / "pages" / "p1" / "visuals" / "v1"
    pages.mkdir(parents=True)
    (pages / "visual.json").write_text(
        json.dumps(
            {
                "visual": {
                    "visualType": "barChart",
                    "visualContainerObjects": {"title": {"not": "a list"}},
                    "query": {
                        "queryState": {
                            "Y": {
                                "projections": [
                                    {
                                        "field": {
                                            "Measure": {
                                                "Expression": {
                                                    "SourceRef": {"Entity": "Sales"}
                                                },
                                                "Property": "Total",
                                            }
                                        },
                                        "queryRef": "Sales.Total",
                                    }
                                ]
                            }
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    visuals = validation_core.parse_report_visuals(str(tmp_path))
    assert len(visuals) == 1
    assert visuals[0].measures[0].property == "Total"


# --- The gates that report failure by raising -------------------------------


def test_a_broken_rdl_subcheck_raises_rather_than_returning_no_issues(monkeypatch):
    """validate_rdl is the gate pbi_paginated_set refuses on. An empty issue
    list means 'save this'. If a sub-check dies, the exception must reach the
    caller — wrapping this in a try/except would hand back a green light."""

    def boom(t, ns):
        raise AttributeError("sub-check exploded")

    monkeypatch.setattr(rdl_builder, "_check_tablix_shape", boom)
    xml = (
        f'<Report xmlns="{rdl_builder.RDL_NS}">'
        "<DataSources><DataSource Name='DS'/></DataSources>"
        "<DataSets><DataSet Name='D'><Query><DataSourceName>DS</DataSourceName>"
        "</Query></DataSet></DataSets>"
        "<ReportSections><ReportSection><Body><ReportItems>"
        "<Tablix Name='T'><DataSetName>D</DataSetName></Tablix>"
        "</ReportItems></Body></ReportSection></ReportSections>"
        "</Report>"
    )
    with pytest.raises(AttributeError):
        rdl_builder.validate_rdl(xml)


def test_malformed_xml_is_an_issue_not_an_empty_issue_list():
    assert rdl_builder.validate_rdl("<Report>")


def test_a_broken_dax_reference_check_raises_rather_than_passing_the_plan(monkeypatch):
    """validate_plan returning [] means 'this plan is sound, build it'. A check
    that cannot run must not be able to produce that."""

    def boom(expr):
        raise KeyError("reference parser exploded")

    monkeypatch.setattr(sm_validate, "extract_references", boom)
    plan = SchemaPlan(
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
                        name="premiumBand",
                        source_column="writtenPremium",
                        data_type="string",
                        dax_expression='IF([writtenPremium] > 0, "pos", "neg")',
                    ),
                ],
            )
        ],
        measures=[
            MeasureSpec(
                name="Total Premium",
                host_table="factpolicy",
                intent="total written premium",
                dax="SUM('factpolicy'[writtenPremium])",
            )
        ],
    )
    schema_tables = {
        "dbo.factpolicy": [("writtenPremium", "decimal"), ("policyKey", "int64")]
    }
    with pytest.raises(KeyError):
        sm_validate.validate_plan(plan, schema_tables)
