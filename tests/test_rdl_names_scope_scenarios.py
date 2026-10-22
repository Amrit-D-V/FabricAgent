from __future__ import annotations

from report import rdl_builder as rb

NS = rb.RDL_NS


def _doc(body_items: str, datasets: str = "", params: str = "") -> str:
    datasets = datasets or (
        '<DataSet Name="Data"><Fields><Field Name="g"><DataField>g</DataField>'
        "</Field></Fields></DataSet>"
    )
    return (
        f'<Report xmlns="{NS}"><DataSources><DataSource Name="d"/></DataSources>'
        f"<DataSets>{datasets}</DataSets>"
        + (f"<ReportParameters>{params}</ReportParameters>" if params else "")
        + f"<Body><ReportItems>{body_items}</ReportItems></Body><Page/></Report>"
    )


_TABLIX = '<Tablix Name="T"><DataSetName>Data</DataSetName></Tablix>'


def _box(name: str, value: str) -> str:
    return (
        f'<Textbox Name="{name}"><Paragraphs><Paragraph><TextRuns><TextRun>'
        f"<Value>{value}</Value></TextRun></TextRuns></Paragraph></Paragraphs></Textbox>"
    )


def _hits(xml: str, needle: str) -> list[str]:
    return [i for i in rb.validate_rdl(xml) if needle in i]


# --- names ---------------------------------------------------------------


def test_two_report_items_with_the_same_name_are_refused():
    xml = _doc(_TABLIX + _box("Card0", "a") + _box("Card0", "b"))
    assert _hits(xml, "'Card0' is used 2 times")


def test_a_textbox_and_a_tablix_cannot_share_a_name():
    xml = _doc(_TABLIX + _box("T", "x"))
    assert _hits(xml, "'T' is used 2 times")


def test_unique_names_pass():
    xml = _doc(_TABLIX + _box("Card0", "a") + _box("Card1", "b"))
    assert not _hits(xml, "is used")


def test_duplicate_dataset_is_refused():
    ds = (
        '<DataSet Name="Data"><Fields><Field Name="g"><DataField>g</DataField></Field></Fields></DataSet>'
        '<DataSet Name="Data"><Fields><Field Name="h"><DataField>h</DataField></Field></Fields></DataSet>'
    )
    assert _hits(_doc(_TABLIX, datasets=ds), "DataSet 'Data' is declared 2 times")


def test_duplicate_field_in_one_dataset_is_refused():
    ds = (
        '<DataSet Name="Data"><Fields><Field Name="g"><DataField>g</DataField></Field>'
        '<Field Name="g"><DataField>g2</DataField></Field></Fields></DataSet>'
    )
    assert _hits(_doc(_TABLIX, datasets=ds), "declares field 'g' 2 times")


def test_report_items_reference_to_a_missing_textbox_is_refused():
    xml = _doc(_TABLIX + _box("Total", "=ReportItems!Subtotal.Value"))
    assert _hits(xml, "ReportItems!Subtotal")


def test_report_items_reference_to_an_existing_textbox_passes():
    xml = _doc(_TABLIX + _box("Subtotal", "1") + _box("Total", "=ReportItems!Subtotal.Value"))
    assert not _hits(xml, "ReportItems!")


# --- scope ---------------------------------------------------------------

_TWO = (
    '<DataSet Name="dsKPI"><Fields><Field Name="Validated"><DataField>Validated</DataField></Field></Fields></DataSet>'
    '<DataSet Name="dsBreakdown"><Fields><Field Name="g"><DataField>g</DataField></Field></Fields></DataSet>'
)
_TABLIX2 = '<Tablix Name="T"><DataSetName>dsBreakdown</DataSetName></Tablix>'


def test_free_textbox_with_unscoped_field_and_two_datasets_is_refused():
    xml = _doc(_TABLIX2 + _box("Card0", "=Fields!Validated.Value"), datasets=_TWO)
    hits = _hits(xml, "outside any data region")
    assert hits and "First(Fields!X.Value" in hits[0]


def test_explicit_scope_satisfies_it():
    xml = _doc(
        _TABLIX2 + _box("Card0", '=First(Fields!Validated.Value, "dsKPI")'), datasets=_TWO
    )
    assert not _hits(xml, "outside any data region")


def test_scope_naming_a_missing_dataset_is_refused():
    xml = _doc(
        _TABLIX2 + _box("Card0", '=Sum(Fields!Validated.Value, "dsNope")'), datasets=_TWO
    )
    assert _hits(xml, "'dsNope', which is not defined")


def test_single_dataset_reports_are_never_second_guessed():
    xml = _doc(_TABLIX + _box("Card0", "=Fields!g.Value"))
    assert not _hits(xml, "outside any data region")


def test_a_field_inside_a_tablix_needs_no_scope():
    inner = (
        '<Tablix Name="T"><DataSetName>dsBreakdown</DataSetName><TablixBody>'
        "<TablixColumns><TablixColumn><Width>1in</Width></TablixColumn></TablixColumns>"
        "<TablixRows><TablixRow><Height>0.25in</Height><TablixCells><TablixCell>"
        f"<CellContents>{_box('c', '=Fields!g.Value')}</CellContents>"
        "</TablixCell></TablixCells></TablixRow></TablixRows></TablixBody>"
        "<TablixColumnHierarchy><TablixMembers><TablixMember/></TablixMembers></TablixColumnHierarchy>"
        "<TablixRowHierarchy><TablixMembers><TablixMember/></TablixMembers></TablixRowHierarchy>"
        "</Tablix>"
    )
    assert not _hits(_doc(inner, datasets=_TWO), "outside any data region")


# --- charts --------------------------------------------------------------


def test_a_chart_without_a_dataset_is_refused():
    xml = _doc(_TABLIX + '<Chart Name="Trend"><ChartData/></Chart>')
    assert _hits(xml, "Chart 'Trend' has no <DataSetName>")


def test_a_chart_over_a_missing_dataset_is_refused():
    xml = _doc(_TABLIX + '<Chart Name="Trend"><DataSetName>dsMonthly</DataSetName><ChartData/></Chart>')
    assert _hits(xml, "missing DataSet 'dsMonthly'")


def test_a_chart_with_nothing_to_plot_is_refused():
    xml = _doc(_TABLIX + '<Chart Name="Trend"><DataSetName>Data</DataSetName></Chart>')
    assert _hits(xml, "no <ChartData>")


# --- parameters (extending the pGBU rule) --------------------------------


def test_multivalue_boolean_parameter_is_refused():
    p = (
        '<ReportParameter Name="pFlag"><DataType>Boolean</DataType>'
        "<MultiValue>true</MultiValue></ReportParameter>"
    )
    assert _hits(_doc(_TABLIX, params=p), "multi-value Boolean")


def test_parameter_without_a_datatype_is_refused():
    p = '<ReportParameter Name="pGBU"><MultiValue>true</MultiValue></ReportParameter>'
    assert _hits(_doc(_TABLIX, params=p), "has no <DataType>")


# --- the builder must stay clean under all of it --------------------------


def test_build_rdl_output_passes_every_new_check():
    from config import PAGINATED_DESIGN

    xml = rb.build_rdl(
        title="Probe",
        subtitle="s",
        workspace="FabricBI-test",
        dataset="kaizen_v7",
        catalog="",
        dax='EVALUATE ROW("Total", 1)',
        layout="table",
        columns=[{"field": "Total"}, {"field": "Other", "format": "0.0"}],
        total_row=True,
        rows_field="",
        cols_field="",
        value_field="",
        landscape=True,
        summary_cards=[{"field": "Total", "label": "Total"}, {"field": "Other", "label": "O"}],
        group_field="",
        page_break_per_group=False,
        design=PAGINATED_DESIGN,
    )
    assert rb.validate_rdl(xml) == []
