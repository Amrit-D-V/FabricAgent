from __future__ import annotations

import pytest

from config import PAGINATED_DESIGN
from report import rdl_builder as rb

NS = rb.RDL_NS


def _build(**kw):
    base = {
        "title": "Probe",
        "subtitle": "s",
        "workspace": "FabricBI-test",
        "dataset": "kaizen_v7",
        "catalog": "",
        "dax": 'EVALUATE ROW("Total", 1)',
        "layout": "table",
        "columns": [{"field": "Total"}, {"field": "Other", "format": "0.0"}],
        "total_row": True,
        "rows_field": "",
        "cols_field": "",
        "value_field": "",
        "landscape": True,
        "summary_cards": [{"field": "Total", "label": "Total"}],
        "group_field": "",
        "page_break_per_group": False,
        "design": PAGINATED_DESIGN,
    }
    base.update(kw)
    return rb.build_rdl(**base)


def test_the_schema_file_ships_with_the_package():
    assert rb._SCHEMA_PATH.is_file(), "report/schemas/ReportDefinition2016.xsd is missing"


def test_the_schema_compiles_once_and_is_cached():
    first = rb._schema()
    assert rb._schema() is first


@pytest.mark.parametrize(
    "kw",
    [
        {},
        {"group_field": "Other", "page_break_per_group": True},
        {"summary_cards": [], "total_row": False},
        {
            "layout": "matrix",
            "rows_field": "Total",
            "cols_field": "Other",
            "value_field": "Total",
            "columns": [{"field": "Total"}, {"field": "Other"}],
        },
        {"landscape": False, "subtitle": ""},
    ],
    ids=["table", "grouped", "bare", "matrix", "portrait"],
)
def test_every_builder_layout_is_schema_valid(kw):
    assert rb.validate_rdl_schema(_build(**kw)) == []


def test_every_builder_layout_passes_full_validation():
    for kw in ({}, {"group_field": "Other"}):
        assert rb.validate_rdl(_build(**kw)) == []


def test_an_unknown_element_is_refused_by_the_schema():
    xml = _build().replace("<Body>", "<Body><NotAnRdlElement/>", 1)
    issues = rb.validate_rdl_schema(xml)
    assert issues and "NotAnRdlElement" in issues[0]


def test_top_level_order_is_the_rank_tables_job_not_the_schemas():
    xml = _build()
    a, b = xml.index("<DataSources>"), xml.index("<DataSets>")
    ds_src = xml[a : xml.index("</DataSources>") + len("</DataSources>")]
    swapped = xml.replace(ds_src, "", 1).replace("</DataSets>", "</DataSets>" + ds_src, 1)
    assert a < b
    assert rb.validate_rdl_schema(swapped) == [], "schema is permissive here by design"
    assert any(i.startswith("Top-level") for i in rb.validate_rdl(swapped))


def test_schema_messages_drop_the_namespace_uri():
    xml = _build().replace("<Body>", "<Body><Bogus/>", 1)
    for issue in rb.validate_rdl_schema(xml):
        assert NS not in issue
        assert issue.startswith("Schema (line ")


def test_schema_issues_are_appended_to_the_shape_checks():
    xml = _build().replace("<Body>", "<Body><Bogus/>", 1)
    issues = rb.validate_rdl(xml)
    assert any(i.startswith("Schema (line ") for i in issues)
    assert issues[-1].startswith("Schema (line "), "schema issues come last"


def test_malformed_xml_is_still_reported_first():
    assert rb.validate_rdl("<Report><Body>")[0].startswith("Not well-formed XML")


def test_designer_namespace_from_report_builder_is_tolerated():
    # build_rdl already binds xmlns:rd and emits rd:* elements, so every other
    # test here is evidence of tolerance; this adds one more designer element
    # of the kind Report Builder writes on save.
    xml = _build().replace("</Report>", "<rd:ReportUnitType>Inch</rd:ReportUnitType></Report>", 1)
    assert "xmlns:rd=" in xml
    assert rb.validate_rdl_schema(xml) == []
