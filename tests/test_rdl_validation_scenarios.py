"""Scenario matrix for the RDL gate, built from a real failure.

On 2026-08-21 FabricBI produced a paginated report that opened in Report Builder
as "index out of bounds" and nothing else — no line number, no tablix name. The
cause was a 5-member row hierarchy over a 3-row TablixBody. pbi_paginated_set
refuses invalid RDL using validate_rdl, so validate_rdl WAS the gate; it just
never looked inside the Tablix.

These pin the three invariants Report Builder enforces at load time, and — just
as important — pin that a legitimately grouped Tablix is not flagged, because a
false positive here blocks every report with subtotals.
"""

from __future__ import annotations

import pytest

from report.rdl_builder import validate_rdl

NS = "http://schemas.microsoft.com/sqlserver/reporting/2016/01/reportdefinition"


def _rdl(
    rows: int,
    cols: int,
    row_members: int | None = None,
    col_members: int | None = None,
    cells_in_first_row: int | None = None,
    grouped: bool = False,
) -> str:
    """A minimal well-formed RDL whose Tablix shape is dialled in by argument."""
    row_members = rows if row_members is None else row_members
    col_members = cols if col_members is None else col_members

    columns = "".join(
        "<TablixColumn><Width>2.5cm</Width></TablixColumn>" for _ in range(cols)
    )

    def _row(r, n_cells):
        cells = "".join(
            f"<TablixCell><CellContents><Textbox Name='t{r}_{i}'>"
            "<Paragraphs><Paragraph><TextRuns><TextRun><Value>x</Value>"
            "</TextRun></TextRuns></Paragraph></Paragraphs>"
            "</Textbox></CellContents></TablixCell>"
            for i in range(n_cells)
        )
        return f"<TablixRow><Height>0.6cm</Height><TablixCells>{cells}</TablixCells></TablixRow>"

    body_rows = "".join(
        _row(
            i,
            cells_in_first_row if (i == 0 and cells_in_first_row is not None) else cols
        )
        for i in range(rows)
    )

    if grouped:
        # One group member wrapping the leaves — the shape a report with
        # subtotals actually has.
        leaves = "".join("<TablixMember />" for _ in range(row_members))
        row_hier = (
            f"<TablixMember><Group Name='g'><GroupExpressions><GroupExpression>"
            f"=Fields!A.Value</GroupExpression></GroupExpressions></Group>"
            f"<TablixMembers>{leaves}</TablixMembers></TablixMember>"
        )
    else:
        row_hier = "".join("<TablixMember />" for _ in range(row_members))

    col_hier = "".join("<TablixMember />" for _ in range(col_members))

    return f"""<?xml version="1.0" encoding="utf-8"?>
<Report xmlns="{NS}">
  <DataSources><DataSource Name="ds"><ConnectionProperties>
    <DataProvider>SQL</DataProvider><ConnectString>x</ConnectString>
  </ConnectionProperties></DataSource></DataSources>
  <DataSets><DataSet Name="Main"><Query><DataSourceName>ds</DataSourceName>
    <CommandText>EVALUATE T</CommandText></Query>
    <Fields><Field Name="A"><DataField>A</DataField></Field></Fields>
  </DataSet></DataSets>
  <ReportSections><ReportSection><Body><ReportItems>
    <Tablix Name="MainTable">
      <TablixBody>
        <TablixColumns>{columns}</TablixColumns>
        <TablixRows>{body_rows}</TablixRows>
      </TablixBody>
      <TablixColumnHierarchy><TablixMembers>{col_hier}</TablixMembers></TablixColumnHierarchy>
      <TablixRowHierarchy><TablixMembers>{row_hier}</TablixMembers></TablixRowHierarchy>
      <DataSetName>Main</DataSetName>
    </Tablix>
  </ReportItems></Body>
  <Page><PageHeight>29.7cm</PageHeight><PageWidth>21cm</PageWidth></Page>
  </ReportSection></ReportSections>
</Report>"""


# --- The shape that shipped and crashed -------------------------------------


def test_a_consistent_tablix_is_valid():
    assert validate_rdl(_rdl(rows=3, cols=9)) == []


def test_the_exact_shape_that_crashed_report_builder_is_refused():
    """5 row-hierarchy members over a 3-row body — what FabricBI generated."""
    issues = validate_rdl(_rdl(rows=3, cols=9, row_members=5))
    assert issues, "the crash shape passed validation"
    assert "index out of bounds" in issues[0]
    assert "5 leaf member(s)" in issues[0] and "3 row(s)" in issues[0]


@pytest.mark.parametrize("members,rows", [(5, 3), (2, 3), (1, 4), (0, 2)])
def test_any_row_hierarchy_mismatch_is_refused(members, rows):
    assert validate_rdl(_rdl(rows=rows, cols=3, row_members=members))


@pytest.mark.parametrize("members,cols", [(9, 3), (2, 5), (0, 1)])
def test_any_column_hierarchy_mismatch_is_refused(members, cols):
    issues = validate_rdl(_rdl(rows=2, cols=cols, col_members=members))
    assert any("column hierarchy" in i for i in issues)


@pytest.mark.parametrize("cells,cols", [(8, 9), (1, 3), (0, 2), (4, 3)])
def test_a_row_with_the_wrong_cell_count_is_refused(cells, cols):
    issues = validate_rdl(_rdl(rows=2, cols=cols, cells_in_first_row=cells))
    assert any("cell(s) for" in i for i in issues)


def test_the_refusal_names_the_tablix():
    """'index out of bounds' in Report Builder names nothing. The refusal must
    say which tablix, or it is no more diagnosable than the crash."""
    issues = validate_rdl(_rdl(rows=3, cols=4, row_members=5))
    assert "MainTable" in issues[0]


# --- False positives would block every real report --------------------------


def test_a_grouped_tablix_is_not_flagged():
    """A group member wraps its rows; the leaves are what render. Counting
    top-level members instead would reject every report with subtotals."""
    assert validate_rdl(_rdl(rows=3, cols=4, row_members=3, grouped=True)) == []


def test_a_grouped_tablix_with_a_real_mismatch_is_still_caught():
    assert validate_rdl(_rdl(rows=3, cols=4, row_members=5, grouped=True))


def test_a_single_row_single_column_tablix_is_valid():
    assert validate_rdl(_rdl(rows=1, cols=1)) == []


# --- The cell check must not fire on shapes where divergence is legal -------
# Learned from data-goblin/power-bi-agentic-development: cells-per-row ==
# column count holds only for a FLAT tablix. Checking it unconditionally
# refuses to save valid matrix reports, because pbi_paginated_set refuses on
# any issue at all.


def test_a_matrix_is_not_flagged_for_cell_count():
    """A column group generates columns at render time, so the static cell
    count legitimately differs from the declared column count."""
    xml = _rdl(rows=2, cols=3, cells_in_first_row=1).replace(
        "<TablixColumnHierarchy><TablixMembers>",
        "<TablixColumnHierarchy><TablixMembers><TablixMember><Group Name='cg'>"
        "<GroupExpressions><GroupExpression>=Fields!A.Value</GroupExpression>"
        "</GroupExpressions></Group></TablixMember>",
        1,
    )
    assert not any("cell(s) for" in i for i in validate_rdl(xml))


def test_a_row_span_is_not_flagged_for_cell_count():
    """A merged cell consumes cells from the rows below it."""
    xml = _rdl(rows=3, cols=4, cells_in_first_row=2).replace(
        "<TablixCell>", "<TablixCell><RowSpan>2</RowSpan>", 1
    )
    assert not any("cell(s) for" in i for i in validate_rdl(xml))


def test_leaf_counts_are_still_enforced_on_a_matrix():
    """The exemption is for the cell check only — the hierarchy invariant that
    actually crashes Report Builder still applies to every tablix."""
    xml = _rdl(rows=3, cols=3, row_members=5).replace(
        "<TablixColumnHierarchy><TablixMembers>",
        "<TablixColumnHierarchy><TablixMembers><TablixMember><Group Name='cg'>"
        "<GroupExpressions><GroupExpression>=Fields!A.Value</GroupExpression>"
        "</GroupExpressions></Group></TablixMember>",
        1,
    )
    assert any("index out of bounds" in i for i in validate_rdl(xml))


# --- Element order is load-bearing ------------------------------------------


def test_datasets_before_datasources_is_refused():
    """The processor fails to load rather than reporting the problem."""
    xml = _rdl(rows=1, cols=1)
    ds_block = xml[xml.index("<DataSources>") : xml.index("</DataSources>") + 14]
    sets_block = xml[xml.index("<DataSets>") : xml.index("</DataSets>") + 11]
    swapped = (
        xml.replace(ds_block, "@@X@@")
        .replace(sets_block, ds_block)
        .replace("@@X@@", sets_block)
    )
    issues = validate_rdl(swapped)
    assert any("load-bearing" in i for i in issues)


def test_the_normal_order_is_accepted():
    assert not any("load-bearing" in i for i in validate_rdl(_rdl(rows=2, cols=2)))


# --- Dimension units --------------------------------------------------------


@pytest.mark.parametrize("bad", ["12px", "12", "100%", "2.5 units"])
def test_a_dimension_without_a_valid_unit_is_refused(bad):
    """px is not RDL. It fails at render, long after the save looked fine."""
    xml = _rdl(rows=1, cols=1).replace("<Width>2.5cm</Width>", f"<Width>{bad}</Width>")
    assert any("need a unit" in i for i in validate_rdl(xml))


@pytest.mark.parametrize(
    "good", ["2.5cm", "1in", "10 mm", "8pt", "3pc", "=Fields!W.Value"]
)
def test_valid_units_and_expressions_pass(good):
    xml = _rdl(rows=1, cols=1).replace("<Width>2.5cm</Width>", f"<Width>{good}</Width>")
    assert not any("need a unit" in i for i in validate_rdl(xml))


# --- Reference integrity ----------------------------------------------------


def test_a_dataset_pointing_at_a_missing_datasource_is_refused():
    xml = _rdl(rows=1, cols=1).replace(
        "<DataSourceName>ds</DataSourceName>", "<DataSourceName>ghost</DataSourceName>"
    )
    assert any("undefined DataSource" in i for i in validate_rdl(xml))


def test_an_embedded_image_with_no_matching_definition_is_refused():
    xml = _rdl(rows=1, cols=1).replace(
        "</ReportItems>",
        "<Image Name='logo'><Source>Embedded</Source><Value>missing_logo</Value>"
        "</Image></ReportItems>",
    )
    assert any("matches no <EmbeddedImage>" in i for i in validate_rdl(xml))


def test_an_expression_driven_image_value_is_not_checked():
    """The image is chosen at runtime; only a literal name is checkable."""
    xml = _rdl(rows=1, cols=1).replace(
        "</ReportItems>",
        "<Image Name='logo'><Source>Embedded</Source>"
        "<Value>=Fields!Flag.Value</Value></Image></ReportItems>",
    )
    assert not any("EmbeddedImage" in i for i in validate_rdl(xml))


# --- The pre-existing checks still work -------------------------------------


def test_malformed_xml_is_reported_first():
    issues = validate_rdl("<Report><unclosed>")
    assert len(issues) == 1 and "well-formed" in issues[0]


def test_a_missing_dataset_is_still_caught():
    xml = _rdl(rows=2, cols=2).replace(
        "<DataSetName>Main</DataSetName>", "<DataSetName>Ghost</DataSetName>"
    )
    assert any("missing DataSet" in i for i in validate_rdl(xml))


def test_a_report_with_no_tablix_is_still_caught():
    xml = _rdl(rows=2, cols=2)
    xml = xml[: xml.index("<Tablix ")] + xml[xml.index("</Tablix>") + 9 :]
    assert any("No <Tablix>" in i for i in validate_rdl(xml))


def test_shape_and_reference_problems_are_reported_together():
    """One round trip per problem costs a turn each."""
    xml = _rdl(rows=3, cols=4, row_members=5, cells_in_first_row=2)
    xml = xml.replace(
        "<DataSetName>Main</DataSetName>", "<DataSetName>Ghost</DataSetName>"
    )
    issues = validate_rdl(xml)
    assert len(issues) >= 3
