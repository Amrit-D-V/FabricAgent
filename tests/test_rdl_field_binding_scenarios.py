from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import pytest

from report import rdl_builder as rb

NS = {"r": rb.RDL_NS}


def _build(**kw):
    base = dict(
        title="School Performance Summary",
        workspace="FabricBI",
        dataset="CMI Analysis-Fabcon 3",
        dax='EVALUATE ROW("Schools", [Number of Schools])',
        columns=[{"field": "Subject"}, {"field": "Meeting Exp %", "format": "0.0"}],
    )
    base.update(kw)
    return rb.build_rdl(**base)


def _declared(xml: str) -> set[str]:
    return {f.get("Name") for f in ET.fromstring(xml).findall(".//r:Field", NS)}


def _referenced(xml: str) -> set[str]:
    root = ET.fromstring(xml)
    return {
        r
        for el in root.iter()
        for r in re.findall(r"Fields!([0-9A-Za-z_]+)", el.text or "")
    }


# --- A KPI card's field is part of the dataset ------------------------------


def test_a_card_field_outside_the_table_is_declared():
    xml = _build(summary_cards=[{"label": "Schools", "field": "Schools"}])
    assert "Schools" in _declared(xml)
    assert rb.validate_rdl(xml) == []


def test_every_referenced_field_is_declared():
    xml = _build(
        summary_cards=[
            {"label": "A", "field": "Schools", "agg": "sum"},
            {"label": "B", "field": "Avg Score", "agg": "avg"},
            {"label": "C", "value": "=Avg(Fields!School_Score.Value)"},
        ]
    )
    assert _referenced(xml) <= _declared(xml)
    assert rb.validate_rdl(xml) == []


def test_a_card_over_a_table_column_is_not_declared_twice():
    xml = _build(summary_cards=[{"label": "S", "field": "Subject"}])
    names = [f.get("Name") for f in ET.fromstring(xml).findall(".//r:Field", NS)]
    assert names.count("Subject") == 1


def test_two_cards_over_one_field_declare_it_once():
    xml = _build(
        summary_cards=[
            {"label": "A", "field": "Schools", "agg": "sum"},
            {"label": "B", "field": "Schools", "agg": "avg"},
        ]
    )
    names = [f.get("Name") for f in ET.fromstring(xml).findall(".//r:Field", NS)]
    assert names.count("Schools") == 1


def test_a_card_carries_the_datafield_the_query_returns():
    xml = _build(summary_cards=[{"label": "M", "field": "Meeting Exp %"}])
    root = ET.fromstring(xml)
    field = next(
        f for f in root.findall(".//r:Field", NS) if f.get("Name") == "Meeting_Exp__"
    )
    assert field.find("r:DataField", NS).text == "Meeting Exp %"


def test_a_matrix_layout_declares_its_card_fields_too():
    xml = _build(
        layout="matrix",
        rows_field="Subject",
        cols_field="Year",
        value_field="Score",
        summary_cards=[{"label": "S", "field": "Schools"}],
    )
    assert _referenced(xml) <= _declared(xml)


# --- validate_rdl refuses an undeclared reference ---------------------------


def test_an_undeclared_reference_is_refused():
    xml = _build(summary_cards=[{"label": "S", "field": "Schools"}])
    broken = xml.replace(
        '<Field Name="Schools"><DataField>Schools</DataField></Field>', ""
    )
    assert broken != xml, "the fixture no longer matches the generated XML"
    issues = rb.validate_rdl(broken)
    assert issues, "an undeclared field reference was accepted"
    assert any("Schools" in i for i in issues)


def test_the_refusal_names_the_textbox_report_builder_would_name():
    xml = _build(summary_cards=[{"label": "S", "field": "Schools"}])
    broken = xml.replace(
        '<Field Name="Schools"><DataField>Schools</DataField></Field>', ""
    )
    assert any("Card0" in i for i in rb.validate_rdl(broken))


def test_a_case_typo_is_refused():
    xml = _build()
    typo = xml.replace("Fields!Subject.Value", "Fields!subject.Value")
    assert typo != xml
    assert rb.validate_rdl(typo)


def test_a_valid_report_is_not_refused():
    assert rb.validate_rdl(_build()) == []


# --- DataFields are checked against the query's real columns ----------------


@pytest.mark.parametrize(
    "cols",
    [
        ["Subject", "Meeting Exp %"],
        ["[Subject]", "[Meeting Exp %]"],
        ["Data[Subject]", "Table[Meeting Exp %]"],
    ],
)
def test_notation_alone_is_not_a_mismatch(cols):
    assert rb.unmatched_data_fields(_build(), cols) == []


def test_a_column_the_query_does_not_return_is_reported():
    missing = rb.unmatched_data_fields(_build(), ["Subject"])
    assert missing == ["Meeting Exp %"]


def test_a_card_field_is_checked_against_the_query_too():
    xml = _build(summary_cards=[{"label": "S", "field": "Schools"}])
    assert "Schools" in rb.unmatched_data_fields(xml, ["Subject", "Meeting Exp %"])


def test_no_known_columns_means_no_opinion():
    assert rb.unmatched_data_fields(_build(), []) == []


def test_unparseable_xml_is_not_reported_as_a_field_mismatch():
    assert rb.unmatched_data_fields("<not-xml", ["A"]) == []


# --- report parameters -------------------------------------------------
# "The report parameter 'pGBU' is a multi value parameter and accepts null
# value. This is not allowed." SSRS rejects the entire report for it and names
# no file, because the definition never loaded. build_rdl emits no parameters,
# so these arrive via pbi_paginated_set where the model writes the XML itself.


def _param_report(params: str) -> str:
    return (
        '<Report xmlns="%s"><DataSources><DataSource Name="d"/></DataSources>'
        '<DataSets><DataSet Name="Data"><Fields>'
        '<Field Name="g"><DataField>g</DataField></Field>'
        "</Fields></DataSet></DataSets>"
        "<ReportParameters>%s</ReportParameters>"
        '<Body><ReportItems><Tablix Name="T">'
        "<DataSetName>Data</DataSetName></Tablix></ReportItems></Body>"
        "<Page/></Report>"
    ) % (rb.RDL_NS, params)


def test_multivalue_plus_nullable_is_refused():
    xml = _param_report(
        '<ReportParameter Name="pGBU"><DataType>String</DataType>'
        "<MultiValue>true</MultiValue><Nullable>true</Nullable></ReportParameter>"
    )
    hits = [i for i in rb.validate_rdl(xml) if "pGBU" in i and "MultiValue" in i]
    assert hits, "the exact SSRS failure must be caught before Report Builder"
    assert "Nullable" in hits[0], "the message must say which half to drop"


def test_multivalue_alone_is_fine():
    xml = _param_report(
        '<ReportParameter Name="pGBU"><DataType>String</DataType>'
        "<MultiValue>true</MultiValue><Nullable>false</Nullable></ReportParameter>"
    )
    assert not [i for i in rb.validate_rdl(xml) if "pGBU" in i and "MultiValue" in i]


def test_nullable_alone_is_fine():
    xml = _param_report(
        '<ReportParameter Name="pMonth"><DataType>String</DataType>'
        "<Nullable>true</Nullable></ReportParameter>"
    )
    assert not [i for i in rb.validate_rdl(xml) if "pMonth" in i]


def test_parameter_valid_values_from_a_missing_dataset():
    xml = _param_report(
        '<ReportParameter Name="pGBU"><DataType>String</DataType><ValidValues>'
        "<DataSetReference><DataSetName>dsGBU</DataSetName></DataSetReference>"
        "</ValidValues></ReportParameter>"
    )
    assert [i for i in rb.validate_rdl(xml) if "dsGBU" in i]


def test_expression_referencing_an_undeclared_parameter():
    xml = _param_report(
        '<ReportParameter Name="pGBU"><DataType>String</DataType></ReportParameter>'
    ).replace(
        "<DataSetName>Data</DataSetName>",
        "<DataSetName>Data</DataSetName><Filters><Filter><FilterExpression>"
        "=Parameters!pMonth.Value</FilterExpression></Filter></Filters>",
    )
    assert [i for i in rb.validate_rdl(xml) if "pMonth" in i]
