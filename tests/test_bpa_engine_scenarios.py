"""Scenario matrix for the in-process Best Practice Analyzer.

BPA previously ran only through Tabular Editor 2, which is .NET Framework with
no Linux build — so on the VM and in every container `pbi_model_bpa` reported
"not available" and the build pipeline's Phase 7.5 skipped. This engine is the
replacement floor, and it is judged on two things at once:

  * it must CATCH the modelling faults TE2 catches, and
  * it must not INVENT any, because pbi_model_bpa output is read as authoritative
    and a rule that cries wolf gets the whole report ignored.

Roughly half of these are false-positive tests for that reason.
"""

from __future__ import annotations

import pytest

from model import bpa_engine
from model.bpa_engine import LOCAL_SAFE_RULES, RULES, evaluate, from_reference_model


def _meta(measures=(), columns=(), relationships=(), tables=()):
    return {
        "measures": list(measures),
        "columns": list(columns),
        "relationships": list(relationships),
        "tables": list(tables),
    }


def _m(name="M", table="Sales", **kw):
    base = {
        "table": table,
        "name": name,
        "expression": "SUM(Sales[Amount])",
        "format_string": "#,0",
        "display_folder": "Core",
        "is_hidden": False,
        "description": "a measure",
    }
    base.update(kw)
    return base


def _c(name="Amount", table="Sales", **kw):
    base = {
        "table": table,
        "name": name,
        "data_type": "int64",
        "is_hidden": False,
        "is_key": False,
        "summarize_by": "none",
        "description": "",
    }
    base.update(kw)
    return base


def _rel(**kw):
    base = {
        "from_table": "Sales",
        "from_column": "DateKey",
        "to_table": "DimDate",
        "to_column": "DateKey",
        "is_active": True,
        "cross_filter": "oneDirection",
        "from_cardinality": "many",
        "to_cardinality": "one",
    }
    base.update(kw)
    return base


def _rules_fired(report):
    return {f["rule"] for key in ("errors", "warnings", "info") for f in report[key]}


def _objects_for(report, rule_fragment):
    out = []
    for key in ("errors", "warnings", "info"):
        for f in report[key]:
            if rule_fragment.lower() in f["rule"].lower():
                out.extend(f["objects"])
    return out


# --- A clean model must produce nothing -------------------------------------


def test_a_clean_model_reports_no_findings():
    """The most important test here. If a well-built model trips a rule, every
    real finding gets ignored as noise."""
    report = evaluate(
        _meta(
            measures=[_m()],
            columns=[_c(), _c(name="DateKey", is_hidden=True)],
            relationships=[_rel()],
            tables=[
                {"name": "Sales", "is_hidden": False},
                {"name": "DimDate", "is_hidden": False},
            ],
        )
    )
    assert report["errors"] == [] and report["warnings"] == [] and report["info"] == []


def test_an_empty_model_does_not_crash():
    report = evaluate(_meta())
    assert report["available"] is True


@pytest.mark.parametrize("bad", [None, {}, {"measures": None}])
def test_degenerate_metadata_does_not_crash(bad):
    assert evaluate(bad)["available"] is True


# --- Measures ---------------------------------------------------------------


def test_a_measure_with_no_format_string_is_flagged():
    report = evaluate(_meta(measures=[_m(format_string="")]))
    assert "Sales[M]" in _objects_for(report, "format string")


def test_a_hidden_measure_is_not_flagged_for_presentation_rules():
    """Hidden measures never appear in the field list, so format string and
    display folder do not matter for them."""
    report = evaluate(
        _meta(
            measures=[
                _m(is_hidden=True, format_string="", display_folder="", description="")
            ]
        )
    )
    fired = _rules_fired(report)
    assert not any(
        x in " ".join(fired).lower()
        for x in ("format string", "display folder", "description")
    )


@pytest.mark.parametrize("expr", ["", "   ", "BLANK()", "blank()", "  BLANK()  "])
def test_a_placeholder_measure_is_an_error(expr):
    report = evaluate(_meta(measures=[_m(expression=expr)]))
    assert "Sales[M]" in _objects_for(report, "empty DAX")


def test_a_real_measure_is_not_a_placeholder():
    report = evaluate(_meta(measures=[_m(expression="CALCULATE(SUM(S[A]))")]))
    assert _objects_for(report, "empty DAX") == []


def test_bare_division_is_flagged():
    report = evaluate(_meta(measures=[_m(expression="[Profit] / [Revenue]")]))
    assert "Sales[M]" in _objects_for(report, "DIVIDE")


def test_divide_function_clears_the_rule():
    report = evaluate(_meta(measures=[_m(expression="DIVIDE([Profit], [Revenue])")]))
    assert _objects_for(report, "DIVIDE") == []


@pytest.mark.parametrize(
    "expr",
    [
        'FORMAT([D], "dd/MM/yyyy")',
        '"a/b"',
        "// ratio = a / b\nSUM(S[A])",
        "/* uses a / b */ SUM(S[A])",
    ],
    ids=["date format", "string literal", "line comment", "block comment"],
)
def test_a_slash_inside_a_string_or_comment_is_not_division(expr):
    """The upstream rule is a bare Contains('/'), which fires on every date
    format string in the model. That is exactly the noise that gets BPA turned
    off."""
    report = evaluate(_meta(measures=[_m(expression=expr)]))
    assert _objects_for(report, "DIVIDE") == []


def test_duplicate_measure_expressions_are_reported_together():
    report = evaluate(
        _meta(
            measures=[
                _m(name="Revenue", expression="SUM(S[A])"),
                _m(name="Total Revenue", expression="SUM(S[A])"),
            ]
        )
    )
    found = _objects_for(report, "identical expression")
    assert len(found) == 1
    assert "Sales[Revenue]" in found[0] and "Sales[Total Revenue]" in found[0]


def test_whitespace_differences_still_count_as_duplicates():
    report = evaluate(
        _meta(
            measures=[
                _m(name="A", expression="SUM( S[A] )"),
                _m(name="B", expression="SUM(S[A])"),
            ]
        )
    )
    assert _objects_for(report, "identical expression")


def test_different_expressions_are_not_duplicates():
    report = evaluate(
        _meta(
            measures=[
                _m(name="A", expression="SUM(S[A])"),
                _m(name="B", expression="SUM(S[B])"),
            ]
        )
    )
    assert _objects_for(report, "identical expression") == []


def test_blank_measures_are_not_reported_as_duplicates_of_each_other():
    """Two placeholders are already an error; calling them duplicates as well
    doubles the noise for one problem."""
    report = evaluate(
        _meta(measures=[_m(name="A", expression=""), _m(name="B", expression="")])
    )
    assert _objects_for(report, "identical expression") == []


# --- Columns ----------------------------------------------------------------


def test_a_visible_foreign_key_column_is_flagged():
    report = evaluate(_meta(columns=[_c(name="DateKey")], relationships=[_rel()]))
    assert "Sales[DateKey]" in _objects_for(report, "Foreign-key")


def test_a_hidden_foreign_key_is_fine():
    report = evaluate(
        _meta(columns=[_c(name="DateKey", is_hidden=True)], relationships=[_rel()])
    )
    assert _objects_for(report, "Foreign-key") == []


def test_a_key_column_is_exempt():
    report = evaluate(
        _meta(columns=[_c(name="DateKey", is_key=True)], relationships=[_rel()])
    )
    assert _objects_for(report, "Foreign-key") == []


def test_a_column_not_in_any_relationship_is_not_a_foreign_key():
    report = evaluate(_meta(columns=[_c(name="Amount")], relationships=[_rel()]))
    assert _objects_for(report, "Foreign-key") == []


@pytest.mark.parametrize("dtype", ["string", "String", "TEXT", "binary"])
def test_a_text_column_that_aggregates_is_flagged(dtype):
    report = evaluate(
        _meta(columns=[_c(name="Name", data_type=dtype, summarize_by="sum")])
    )
    assert "Sales[Name]" in _objects_for(report, "Text columns")


@pytest.mark.parametrize("summarize", ["none", "None", "default", ""])
def test_a_text_column_that_does_not_aggregate_is_fine(summarize):
    report = evaluate(
        _meta(columns=[_c(name="Name", data_type="string", summarize_by=summarize)])
    )
    assert _objects_for(report, "Text columns") == []


def test_a_numeric_column_that_aggregates_is_fine():
    report = evaluate(_meta(columns=[_c(data_type="int64", summarize_by="sum")]))
    assert _objects_for(report, "Text columns") == []


# --- Relationships ----------------------------------------------------------


@pytest.mark.parametrize("cf", ["both", "bothDirections", "BOTH", "2"])
def test_a_bidirectional_relationship_is_an_error(cf):
    report = evaluate(_meta(relationships=[_rel(cross_filter=cf)]))
    assert _objects_for(report, "bi-directional")


@pytest.mark.parametrize("cf", ["oneDirection", "single", "1", ""])
def test_a_single_direction_relationship_is_fine(cf):
    report = evaluate(_meta(relationships=[_rel(cross_filter=cf)]))
    assert _objects_for(report, "bi-directional") == []


def test_many_to_many_is_an_error():
    report = evaluate(
        _meta(relationships=[_rel(from_cardinality="many", to_cardinality="many")])
    )
    assert _objects_for(report, "many-to-many")


def test_many_to_one_is_fine():
    report = evaluate(_meta(relationships=[_rel()]))
    assert _objects_for(report, "many-to-many") == []


def test_an_inactive_relationship_no_measure_uses_is_flagged():
    report = evaluate(
        _meta(
            measures=[_m(expression="SUM(S[A])")], relationships=[_rel(is_active=False)]
        )
    )
    assert _objects_for(report, "Inactive relationship")


def test_an_inactive_relationship_activated_by_userelationship_is_fine():
    report = evaluate(
        _meta(
            measures=[
                _m(
                    expression="CALCULATE(SUM(S[A]), USERELATIONSHIP(Sales[DateKey], DimDate[DateKey]))"
                )
            ],
            relationships=[_rel(is_active=False)],
        )
    )
    assert _objects_for(report, "Inactive relationship") == []


def test_an_active_relationship_is_never_flagged_as_unused():
    report = evaluate(_meta(relationships=[_rel(is_active=True)]))
    assert _objects_for(report, "Inactive relationship") == []


# --- Tables -----------------------------------------------------------------


def test_a_table_with_no_relationships_is_flagged():
    report = evaluate(
        _meta(tables=[{"name": "Orphan", "is_hidden": False}], relationships=[_rel()])
    )
    assert "Orphan" in _objects_for(report, "no relationships")


@pytest.mark.parametrize(
    "name", ["DimDate", "DateTable", "Date", "Calendar", "_staging"]
)
def test_date_and_helper_tables_are_exempt(name):
    """A marked date table is legitimately unrelated in some models. Flagging it
    every run teaches the reader to skip the section."""
    report = evaluate(_meta(tables=[{"name": name, "is_hidden": False}]))
    assert _objects_for(report, "no relationships") == []


def test_a_hidden_table_is_exempt():
    report = evaluate(_meta(tables=[{"name": "Bridge", "is_hidden": True}]))
    assert _objects_for(report, "no relationships") == []


# --- Engine mechanics -------------------------------------------------------


def test_severities_are_ordered_correctly():
    report = evaluate(
        _meta(
            measures=[_m(expression="")],  # error
            relationships=[_rel(cross_filter="both")],  # error
            columns=[_c(name="X", data_type="string", summarize_by="sum")],  # warning
        )
    )
    assert len(report["errors"]) == 2
    assert any("Text columns" in f["rule"] for f in report["warnings"])


def test_one_broken_rule_does_not_lose_the_others(monkeypatch):
    """A single malformed field must not cost the whole audit."""

    def boom(meta):
        raise ValueError("bad field")

    patched = [
        (rid, n, s, boom if rid == "MEASURES_HAVE_FORMAT_STRING" else fn)
        for rid, n, s, fn in RULES
    ]
    monkeypatch.setattr(bpa_engine, "RULES", patched)
    report = evaluate(_meta(measures=[_m(expression="")]))
    assert any("rule errored" in f["rule"] for f in report["info"])
    assert report["errors"], "other rules must still run"


def test_only_filter_restricts_which_rules_run():
    report = evaluate(
        _meta(measures=[_m(format_string="", expression="")]),
        only={"MEASURES_NO_DAX_EXPRESSION"},
    )
    fired = _rules_fired(report)
    assert any("empty DAX" in f for f in fired)
    assert not any("format string" in f for f in fired)


def test_the_summary_says_how_many_rules_actually_ran():
    """A filtered run that claimed all 12 ran would overstate the audit."""
    full = evaluate(_meta())
    partial = evaluate(_meta(), only={"MEASURES_NO_DAX_EXPRESSION"})
    assert f"{len(RULES)} of {len(RULES)}" in full["summary"]
    assert f"1 of {len(RULES)}" in partial["summary"]


def test_local_safe_rules_are_all_real_rule_ids():
    """A typo here would silently disable a rule in the build pipeline."""
    assert LOCAL_SAFE_RULES <= {r[0] for r in RULES}


def test_local_safe_rules_exclude_everything_needing_absent_fields():
    """The PBIP loader exposes no IsKey, SummarizeBy, measure IsHidden or
    descriptions. Rules depending on those must not run locally, or a missing
    field reads as False and invents violations."""
    needs_absent = {
        "COLUMNS_HIDE_FK",  # is_key
        "COLUMNS_TEXT_SHOULD_NOT_AGGREGATE",  # summarize_by
        "MEASURES_HAVE_DESCRIPTION",  # description
        "RELATIONSHIPS_MANY_TO_MANY",  # cardinalities
    }
    assert not (LOCAL_SAFE_RULES & needs_absent)


# --- The PBIP adapter -------------------------------------------------------


class _RefCol:
    def __init__(self, name, is_hidden=False, data_type="string"):
        self.name, self.is_hidden, self.data_type = name, is_hidden, data_type


class _RefTable:
    def __init__(self, name, cols=(), is_hidden=False):
        self.name, self.columns, self.is_hidden = name, list(cols), is_hidden


class _RefMeasure:
    def __init__(self, name, host_table, dax, format_string=None, display_folder=None):
        self.name, self.host_table, self.dax = name, host_table, dax
        self.format_string, self.display_folder = format_string, display_folder


class _RefRel:
    def __init__(self, ft, fc, tt, tc, is_active=True, cross_filter="singleDirection"):
        self.from_table, self.from_column = ft, fc
        self.to_table, self.to_column = tt, tc
        self.is_active, self.cross_filter = is_active, cross_filter


class _Ref:
    def __init__(self, tables=(), measures=(), rels=()):
        self.business_tables = list(tables)
        self.measures = list(measures)
        self.relationships = list(rels)


def test_the_adapter_maps_a_reference_model():
    ref = _Ref(
        tables=[_RefTable("Sales", [_RefCol("Amount")])],
        measures=[_RefMeasure("Revenue", "Sales", "SUM(Sales[Amount])", "#,0", "Core")],
        rels=[_RefRel("Sales", "DateKey", "DimDate", "DateKey")],
    )
    meta = from_reference_model(ref)
    assert meta["measures"][0]["expression"] == "SUM(Sales[Amount])"
    assert meta["columns"][0]["table"] == "Sales"
    assert meta["relationships"][0]["to_table"] == "DimDate"


def test_the_adapter_turns_none_into_empty_strings():
    """format_string and display_folder are Optional on the loader; None would
    make _blank() work by accident and str() render 'None'."""
    ref = _Ref(measures=[_RefMeasure("M", "Sales", "SUM(S[A])", None, None)])
    meta = from_reference_model(ref)
    assert meta["measures"][0]["format_string"] == ""
    assert meta["measures"][0]["display_folder"] == ""


def test_an_empty_reference_model_adapts_cleanly():
    meta = from_reference_model(_Ref())
    assert evaluate(meta, only=LOCAL_SAFE_RULES)["available"] is True


def test_the_local_path_finds_a_placeholder_measure():
    """End to end for the build pipeline: a BLANK() measure in a PBIP is caught
    without Tabular Editor."""
    ref = _Ref(
        tables=[_RefTable("Sales", [_RefCol("Amount")])],
        measures=[_RefMeasure("Broken", "Sales", "BLANK()", "#,0", "Core")],
    )
    report = evaluate(from_reference_model(ref), only=LOCAL_SAFE_RULES)
    assert "Sales[Broken]" in _objects_for(report, "empty DAX")
