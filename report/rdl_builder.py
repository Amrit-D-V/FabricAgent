from __future__ import annotations

import re
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape as _x

RDL_NS = "http://schemas.microsoft.com/sqlserver/reporting/2016/01/reportdefinition"
RD_NS = "http://schemas.microsoft.com/SQLServer/reporting/reportdesigner"

_DEFAULT_DESIGN = {
    "header_bg": "#1F3864",
    "header_text": "White",
    "zebra_bg": "#F2F2F2",
    "title_color": "#1F3864",
    "subtitle_color": "#595959",
    "border_color": "#BFBFBF",
    "total_bg": "#D9E1F2",
    "group_bg": "#DDE3F0",
    "pos_color": "#006100",
    "neg_color": "#C00000",
}

_CARD_AGGS = {
    "sum": "Sum",
    "avg": "Avg",
    "average": "Avg",
    "count": "Count",
    "countdistinct": "CountDistinct",
    "min": "Min",
    "max": "Max",
    "first": "First",
}


_PBI_IDENTITY_PROVIDER = (
    "https://login.microsoftonline.com/organizations, "
    "https://analysis.windows.net/powerbi/api, "
    "f0b72488-7082-488a-a7e8-eada97bd842d"
)


def _pbi_connect_string(catalog: str) -> str:
    return (
        "Data Source=pbiazure://api.powerbi.com/;"
        f'Identity Provider="{_PBI_IDENTITY_PROVIDER}";'
        f"Initial Catalog={catalog};Integrated Security=ClaimsToken"
    )


def _datasource(workspace: str, dataset: str, catalog: str) -> str:
    return (
        f'<DataSource Name="ModelDS">'
        f"<rd:SecurityType>None</rd:SecurityType>"
        f"<ConnectionProperties>"
        f"<DataProvider>PBIDATASET</DataProvider>"
        f"<ConnectString>{_x(_pbi_connect_string(catalog))}</ConnectString>"
        f"</ConnectionProperties>"
        f"<rd:DataSourceID>{uuid.uuid4()}</rd:DataSourceID>"
        f"<rd:PowerBIWorkspaceName>{_x(workspace)}</rd:PowerBIWorkspaceName>"
        f"<rd:PowerBIDatasetName>{_x(dataset)}</rd:PowerBIDatasetName>"
        f"</DataSource>"
    )


def _ident(name: str) -> str:
    s = re.sub(r"[^0-9A-Za-z_]", "_", (name or "").strip()).lstrip("_")
    if not s or s[0].isdigit():
        s = "F" + s
    return s


def _norm_columns(columns: list[dict]) -> list[dict]:
    out = []
    for c in columns:
        field = (c.get("field") or c.get("header") or "").strip()
        fmt = (c.get("format") or "").strip()
        total = (c.get("total") or "").strip().lower()
        out.append(
            {
                "field": field,
                "name": _ident(field),
                "header": (c.get("header") or field).strip(),
                "format": fmt,
                "total": total,
                "numeric": bool(fmt or total),
                "conditional": (c.get("conditional") or "").strip().lower(),
            }
        )
    return out


def _dataset(dax: str, cols: list[dict]) -> str:
    fields = "".join(
        f'<Field Name="{_x(c["name"])}"><DataField>{_x(c["field"])}</DataField></Field>'
        for c in cols
    )
    return (
        f'<DataSet Name="Data">'
        f"<Query><DataSourceName>ModelDS</DataSourceName>"
        f"<CommandType>Text</CommandType>"
        f"<CommandText>{_x(dax)}</CommandText></Query>"
        f"<Fields>{fields}</Fields>"
        f"</DataSet>"
    )


def _cellbox(
    name, value, design, *, bold=False, align="", bg="", fmt="", color="", border=True
):
    rstyle = ""
    if bold:
        rstyle += "<FontWeight>Bold</FontWeight>"
    if color:
        rstyle += f"<Color>{_x(color)}</Color>"
    if fmt:
        rstyle += f"<Format>{_x(fmt)}</Format>"
    para_style = f"<Style><TextAlign>{align}</TextAlign></Style>" if align else ""
    cstyle = ""
    if border:
        cstyle += f'<Border><Color>{design["border_color"]}</Color><Style>Solid</Style></Border>'
    if bg:
        cstyle += f"<BackgroundColor>{_x(bg)}</BackgroundColor>"
    cstyle += (
        "<PaddingLeft>3pt</PaddingLeft><PaddingRight>3pt</PaddingRight>"
        "<PaddingTop>2pt</PaddingTop><PaddingBottom>2pt</PaddingBottom>"
    )
    return (
        f'<Textbox Name="{_x(name)}"><CanGrow>true</CanGrow><KeepTogether>true</KeepTogether>'
        f"<Paragraphs><Paragraph><TextRuns><TextRun><Value>{_x(value)}</Value>"
        f"<Style>{rstyle}</Style></TextRun></TextRuns>{para_style}</Paragraph></Paragraphs>"
        f"<rd:DefaultName>{_x(name)}</rd:DefaultName>"
        f"<Style>{cstyle}</Style></Textbox>"
    )


def _cell(content: str) -> str:
    return f"<TablixCell><CellContents>{content}</CellContents></TablixCell>"


def _free_textbox(
    name, value, top, left, w, h, *, size_pt=10, bold=False, color="", align=""
):
    st = f"<FontSize>{size_pt}pt</FontSize>"
    if bold:
        st += "<FontWeight>Bold</FontWeight>"
    if color:
        st += f"<Color>{color}</Color>"
    para_style = f"<Style><TextAlign>{align}</TextAlign></Style>" if align else ""
    return (
        f'<Textbox Name="{_x(name)}"><CanGrow>true</CanGrow>'
        f"<Paragraphs><Paragraph><TextRuns><TextRun><Value>{_x(value)}</Value>"
        f"<Style>{st}</Style></TextRun></TextRuns>{para_style}</Paragraph></Paragraphs>"
        f"<rd:DefaultName>{_x(name)}</rd:DefaultName>"
        f"<Top>{top}in</Top><Left>{left}in</Left><Height>{h}in</Height><Width>{w}in</Width><Style /></Textbox>"
    )


def _card_value_expr(card: dict) -> str:
    raw = (card.get("value") or "").strip()
    if raw:
        return raw
    fld = (card.get("field") or "").strip()
    if not fld:
        return ""
    agg = _CARD_AGGS.get((card.get("agg") or "sum").strip().lower(), "Sum")
    return f'={agg}(Fields!{_ident(fld)}.Value, "Data")'


def _kpi_card(name, label, value_expr, fmt, top, left, w, h, design) -> str:
    vstyle = (
        "<FontSize>20pt</FontSize><FontWeight>Bold</FontWeight>"
        f'<Color>{design["title_color"]}</Color>'
    )
    if fmt:
        vstyle += f"<Format>{_x(fmt)}</Format>"
    lstyle = f'<FontSize>9pt</FontSize><Color>{design["subtitle_color"]}</Color>'
    cstyle = (
        f'<Border><Color>{design["border_color"]}</Color><Style>Solid</Style></Border>'
        f'<TopBorder><Color>{design["header_bg"]}</Color><Style>Solid</Style><Width>2.5pt</Width></TopBorder>'
        "<BackgroundColor>White</BackgroundColor>"
        "<PaddingLeft>4pt</PaddingLeft><PaddingRight>4pt</PaddingRight>"
        "<PaddingTop>5pt</PaddingTop><PaddingBottom>3pt</PaddingBottom>"
    )
    return (
        f'<Textbox Name="{_x(name)}"><CanGrow>true</CanGrow><KeepTogether>true</KeepTogether>'
        "<Paragraphs>"
        f"<Paragraph><TextRuns><TextRun><Value>{_x(value_expr)}</Value><Style>{vstyle}</Style></TextRun></TextRuns>"
        "<Style><TextAlign>Center</TextAlign></Style></Paragraph>"
        f"<Paragraph><TextRuns><TextRun><Value>{_x(label)}</Value><Style>{lstyle}</Style></TextRun></TextRuns>"
        "<Style><TextAlign>Center</TextAlign></Style></Paragraph>"
        "</Paragraphs>"
        f"<rd:DefaultName>{_x(name)}</rd:DefaultName>"
        f"<Top>{top}in</Top><Left>{left}in</Left><Height>{h}in</Height><Width>{w}in</Width>"
        f"<Style>{cstyle}</Style></Textbox>"
    )


def _card_columns(cards: list[dict] | None, existing: list[dict]) -> list[dict]:
    have = {c["name"] for c in existing}
    out: list[dict] = []
    for c in cards or []:
        if not c:
            continue
        wanted: list[str] = []
        fld = (c.get("field") or "").strip()
        if fld:
            wanted.append(fld)
        # Fields!Name.Value inside a hand-written expression.
        wanted += re.findall(r"Fields!([0-9A-Za-z_]+)", c.get("value") or "")
        for w in wanted:
            ident = _ident(w)
            if ident in have:
                continue
            have.add(ident)
            out.append(_norm_columns([{"field": w}])[0])
    return out


def _kpi_band(
    cards: list[dict] | None, body_w_in: float, design: dict
) -> tuple[str, float]:
    cards = [c for c in (cards or []) if c][:5]
    n = len(cards)
    if not n:
        return "", 0.0
    gap, h = 0.12, 0.9
    cw = round((body_w_in - gap * (n - 1)) / n, 3)
    items = ""
    for i, c in enumerate(cards):
        left = round(i * (cw + gap), 3)
        items += _kpi_card(
            f"Card{i}",
            c.get("label", ""),
            _card_value_expr(c),
            (c.get("format") or "").strip(),
            0,
            left,
            cw,
            h,
            design,
        )
    return items, round(h + 0.18, 3)


def _agg_row(prefix: str, label: str, cols: list[dict], design: dict, bg: str) -> str:
    cells = ""
    for i, c in enumerate(cols):
        if i == 0:
            tb = _cellbox(f"{prefix}_{c['name']}", label, design, bold=True, bg=bg)
        elif c["total"] == "sum":
            tb = _cellbox(
                f"{prefix}_{c['name']}",
                f"=Sum(Fields!{c['name']}.Value)",
                design,
                bold=True,
                align="Right",
                bg=bg,
                fmt=c["format"],
            )
        else:
            tb = _cellbox(f"{prefix}_{c['name']}", "", design, bg=bg)
        cells += _cell(tb)
    return f"<TablixRow><Height>0.28in</Height><TablixCells>{cells}</TablixCells></TablixRow>"


def _table_tablix(
    cols: list[dict],
    body_w_in: float,
    total_row: bool,
    design: dict,
    *,
    top_in: float = 0.0,
    group_col: dict | None = None,
    page_break_per_group: bool = False,
) -> str:
    n = max(1, len(cols))
    col_w = round(body_w_in / n, 3)
    tablix_cols = "".join(
        f"<TablixColumn><Width>{col_w}in</Width></TablixColumn>" for _ in cols
    )

    header_cells = "".join(
        _cell(
            _cellbox(
                f"h_{c['name']}",
                c["header"],
                design,
                bold=True,
                align=("Right" if c["numeric"] else "Left"),
                bg=design["header_bg"],
                color=design["header_text"],
            )
        )
        for c in cols
    )
    zebra = f'=IIF(RowNumber(Nothing) Mod 2 = 0, "{design["zebra_bg"]}", "White")'

    def _detail_color(c: dict) -> str:
        if c["conditional"] == "posneg":
            return f'=IIF(Fields!{c["name"]}.Value < 0, "{design["neg_color"]}", "{design["pos_color"]}")'
        return ""

    detail_cells = "".join(
        _cell(
            _cellbox(
                f"d_{c['name']}",
                f"=Fields!{c['name']}.Value",
                design,
                align=("Right" if c["numeric"] else "Left"),
                bg=zebra,
                fmt=c["format"],
                color=_detail_color(c),
            )
        )
        for c in cols
    )
    header_row = f"<TablixRow><Height>0.28in</Height><TablixCells>{header_cells}</TablixCells></TablixRow>"
    detail_row = f"<TablixRow><Height>0.26in</Height><TablixCells>{detail_cells}</TablixCells></TablixRow>"
    col_members = "".join("<TablixMember />" for _ in cols)

    if group_col is not None:
        gname = group_col["name"]
        gh_cells = "".join(
            _cell(
                _cellbox(
                    f"gh_{c['name']}",
                    (f"=Fields!{gname}.Value" if i == 0 else ""),
                    design,
                    bold=(i == 0),
                    bg=design["group_bg"],
                )
            )
            for i, c in enumerate(cols)
        )
        group_header_row = f"<TablixRow><Height>0.26in</Height><TablixCells>{gh_cells}</TablixCells></TablixRow>"
        rows = (
            header_row
            + group_header_row
            + detail_row
            + _agg_row("st", "Subtotal", cols, design, design["total_bg"])
        )
        sort = (
            f"<SortExpressions><SortExpression><Value>=Fields!{gname}.Value</Value>"
            "</SortExpression></SortExpressions>"
        )
        pbreak = (
            "<PageBreak><BreakLocation>Between</BreakLocation></PageBreak>"
            if page_break_per_group
            else ""
        )
        row_members = (
            "<TablixMember><KeepWithGroup>After</KeepWithGroup></TablixMember>"
            f'<TablixMember><Group Name="Grp"><GroupExpressions><GroupExpression>'
            f"=Fields!{gname}.Value</GroupExpression></GroupExpressions>{pbreak}</Group>{sort}"
            "<TablixMembers>"
            "<TablixMember><KeepWithGroup>After</KeepWithGroup></TablixMember>"
            '<TablixMember><Group Name="Details" /></TablixMember>'
            "<TablixMember><KeepWithGroup>Before</KeepWithGroup></TablixMember>"
            "</TablixMembers></TablixMember>"
        )
        if total_row:
            rows += _agg_row("gt", "Grand Total", cols, design, design["total_bg"])
            row_members += (
                "<TablixMember><KeepWithGroup>Before</KeepWithGroup></TablixMember>"
            )
    else:
        rows = header_row + detail_row
        row_members = (
            "<TablixMember><KeepWithGroup>After</KeepWithGroup></TablixMember>"
            '<TablixMember><Group Name="Details" /></TablixMember>'
        )
        if total_row:
            rows += _agg_row("t", "Total", cols, design, design["total_bg"])
            row_members += (
                "<TablixMember><KeepWithGroup>Before</KeepWithGroup></TablixMember>"
            )

    return (
        '<Tablix Name="Table1">'
        f"<TablixBody><TablixColumns>{tablix_cols}</TablixColumns>"
        f"<TablixRows>{rows}</TablixRows></TablixBody>"
        f"<TablixColumnHierarchy><TablixMembers>{col_members}</TablixMembers></TablixColumnHierarchy>"
        f"<TablixRowHierarchy><TablixMembers>{row_members}</TablixMembers></TablixRowHierarchy>"
        "<DataSetName>Data</DataSetName>"
        f"<Top>{top_in}in</Top><Left>0in</Left><Height>0.5in</Height><Width>{round(col_w*n, 3)}in</Width></Tablix>"
    )


def _matrix_tablix(
    rows_field: str,
    cols_field: str,
    value_field: str,
    body_w_in: float,
    design: dict,
    *,
    top_in: float = 0.0,
) -> str:
    rf, cf, vf = _ident(rows_field), _ident(cols_field), _ident(value_field)
    half = round(body_w_in / 2, 3)

    def hdr(nm, val):
        return _cellbox(
            nm,
            val,
            design,
            bold=True,
            bg=design["header_bg"],
            color=design["header_text"],
        )

    return (
        '<Tablix Name="Matrix1">'
        "<TablixBody>"
        f"<TablixColumns><TablixColumn><Width>{half}in</Width></TablixColumn>"
        f"<TablixColumn><Width>{half}in</Width></TablixColumn></TablixColumns>"
        "<TablixRows>"
        "<TablixRow><Height>0.28in</Height><TablixCells>"
        f"{_cell(hdr('corner', ''))}{_cell(hdr('colhdr', f'=Fields!{cf}.Value'))}"
        "</TablixCells></TablixRow>"
        "<TablixRow><Height>0.26in</Height><TablixCells>"
        f"{_cell(hdr('rowhdr', f'=Fields!{rf}.Value'))}"
        f"{_cell(_cellbox('val', f'=Fields!{vf}.Value', design, align='Right'))}"
        "</TablixCells></TablixRow>"
        "</TablixRows>"
        "</TablixBody>"
        "<TablixColumnHierarchy><TablixMembers><TablixMember />"
        f'<TablixMember><Group Name="ColGrp"><GroupExpressions><GroupExpression>=Fields!{cf}.Value</GroupExpression></GroupExpressions></Group>'
        f"<SortExpressions><SortExpression><Value>=Fields!{cf}.Value</Value></SortExpression></SortExpressions></TablixMember>"
        "</TablixMembers></TablixColumnHierarchy>"
        "<TablixRowHierarchy><TablixMembers><TablixMember />"
        f'<TablixMember><Group Name="RowGrp"><GroupExpressions><GroupExpression>=Fields!{rf}.Value</GroupExpression></GroupExpressions></Group>'
        f"<SortExpressions><SortExpression><Value>=Fields!{rf}.Value</Value></SortExpression></SortExpressions></TablixMember>"
        "</TablixMembers></TablixRowHierarchy>"
        "<DataSetName>Data</DataSetName>"
        f"<Top>{top_in}in</Top><Left>0in</Left><Height>0.5in</Height><Width>{body_w_in}in</Width>"
        "</Tablix>"
    )


def _page_header(title: str, subtitle: str, body_w: float, design: dict) -> str:
    left_w = round(body_w * 0.62, 3)
    right_w = round(body_w * 0.38, 3)
    items = _free_textbox(
        "HdrTitle",
        title,
        0.08,
        0,
        left_w,
        0.32,
        size_pt=18,
        bold=True,
        color=design["title_color"],
    )
    if subtitle:
        items += _free_textbox(
            "HdrSubtitle",
            subtitle,
            0.46,
            0,
            left_w,
            0.24,
            size_pt=10,
            color=design["subtitle_color"],
        )
    items += _free_textbox(
        "HdrDate",
        '="Generated " & Format(Today(), "dd MMM yyyy")',
        0.12,
        left_w,
        right_w,
        0.24,
        size_pt=10,
        color=design["subtitle_color"],
        align="Right",
    )
    return (
        "<PageHeader><Height>0.85in</Height>"
        "<PrintOnFirstPage>true</PrintOnFirstPage><PrintOnLastPage>true</PrintOnLastPage>"
        f"<ReportItems>{items}</ReportItems><Style /></PageHeader>"
    )


def _page_footer(body_w: float, design: dict) -> str:
    half = round(body_w * 0.5, 3)
    items = _free_textbox(
        "FtrPage",
        '="Page " & Globals!PageNumber & " of " & Globals!TotalPages',
        0.04,
        0,
        half,
        0.2,
        size_pt=9,
        color=design["subtitle_color"],
    )
    items += _free_textbox(
        "FtrTime",
        '=Format(Now(), "dd MMM yyyy HH:mm")',
        0.04,
        half,
        half,
        0.2,
        size_pt=9,
        color=design["subtitle_color"],
        align="Right",
    )
    return (
        "<PageFooter><Height>0.3in</Height>"
        "<PrintOnFirstPage>true</PrintOnFirstPage><PrintOnLastPage>true</PrintOnLastPage>"
        f"<ReportItems>{items}</ReportItems><Style /></PageFooter>"
    )


def build_rdl(
    *,
    title: str,
    workspace: str,
    dataset: str,
    dax: str,
    catalog: str = "",
    layout: str = "table",
    columns: list[dict] | None = None,
    subtitle: str = "",
    total_row: bool = False,
    rows_field: str = "",
    cols_field: str = "",
    value_field: str = "",
    page_width_in: float = 8.5,
    page_height_in: float = 11.0,
    margin_in: float = 1.0,
    landscape: bool = False,
    summary_cards: list[dict] | None = None,
    group_field: str = "",
    page_break_per_group: bool = False,
    design: dict | None = None,
) -> str:
    design = design or _DEFAULT_DESIGN
    if landscape:
        page_width_in, page_height_in = page_height_in, page_width_in
    body_w = round(page_width_in - 2 * margin_in, 3)

    band_xml, band_h = _kpi_band(summary_cards, body_w, design)

    if layout == "matrix":
        item = _matrix_tablix(
            rows_field, cols_field, value_field, body_w, design, top_in=band_h
        )
        ds_cols = _norm_columns(
            [{"field": rows_field}, {"field": cols_field}, {"field": value_field}]
        )
    else:
        cols = _norm_columns(columns or [])
        group_col = None
        ds_cols = cols
        if group_field:
            gid = _ident(group_field)
            group_col = next(
                (c for c in cols if c["field"] == group_field or c["name"] == gid), None
            )
            if group_col is None:
                group_col = _norm_columns([{"field": group_field}])[0]
                ds_cols = cols + [group_col]
            else:
                cols = [c for c in cols if c is not group_col]
        item = _table_tablix(
            cols,
            body_w,
            total_row,
            design,
            top_in=band_h,
            group_col=group_col,
            page_break_per_group=page_break_per_group,
        )

    # A KPI card aggregates a field of the same dataset, so that field has to be
    # declared alongside the table's own columns. It was not: <Fields> came only
    # from `columns`, and a card over anything not also in the table produced
    # "The 'Value' expression for the text box 'Card0' refers to the field 'X'.
    # Report item expressions can only refer to fields within the current
    # dataset scope" -- at render time, from Report Builder, long after the tool
    # had reported success.
    ds_cols = ds_cols + _card_columns(summary_cards, ds_cols)

    body_h = round(band_h + 6.0, 3)
    body = f"<Body><ReportItems>{band_xml}{item}</ReportItems><Height>{body_h}in</Height><Style /></Body>"
    page = (
        "<Page>"
        f"{_page_header(title, subtitle, body_w, design)}"
        f"{_page_footer(body_w, design)}"
        f"<PageHeight>{page_height_in}in</PageHeight><PageWidth>{page_width_in}in</PageWidth>"
        f"<LeftMargin>{margin_in}in</LeftMargin><RightMargin>{margin_in}in</RightMargin>"
        f"<TopMargin>{margin_in}in</TopMargin><BottomMargin>{margin_in}in</BottomMargin>"
        "<Style /></Page>"
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<Report xmlns="{RDL_NS}" xmlns:rd="{RD_NS}">'
        "<AutoRefresh>0</AutoRefresh>"
        f"<DataSources>{_datasource(workspace, dataset, catalog or dataset)}</DataSources>"
        f"<DataSets>{_dataset(dax, ds_cols)}</DataSets>"
        "<ReportSections><ReportSection>"
        f"{body}<Width>{body_w}in</Width>{page}"
        "</ReportSection></ReportSections>"
        f"<rd:ReportID>{uuid.uuid4()}</rd:ReportID>"
        "</Report>"
    )


# The structural checks below were derived from the RDL invariants documented in
# data-goblin/power-bi-agentic-development (GPL-3.0, Kurt Buhler / Data Goblins),
# commit f8495e76. Re-implemented against FabricBI's tools: their version is a CLI
# the agent is told to remember to run, ours is server-side inside
# pbi_paginated_set, so an invalid .rdl cannot reach disk in the first place.

_DIMENSION_TAGS = {
    "Width",
    "Height",
    "Top",
    "Left",
    "PageHeight",
    "PageWidth",
    "InteractiveHeight",
    "InteractiveWidth",
    "LeftMargin",
    "RightMargin",
    "TopMargin",
    "BottomMargin",
}
# RDL sizes are in/cm/mm/pt/pc. px is NOT valid and fails at render, not load.
_DIMENSION_RE = re.compile(r"^-?\d*\.?\d+\s*(in|cm|mm|pt|pc)$")

# Items with their own coordinate space: dimensions inside them are unitless
# numbers, so the unit check must not descend into them.
_COORD_SPACE_TAGS = {"Chart", "Gauge", "GaugePanel", "Map", "CustomReportItem"}

# Rank of top-level <Report> children. Only the load-bearing sequence is
# enforced (sources -> datasets -> sections -> parameters). Leading metadata
# (rd:ReportID, df:DefaultFontFamily, Description...) is deliberately unranked:
# genuine Report Builder output varies its position, so ranking it would reject
# real reports. Trailing metadata shares one rank, leaving its mutual order free.
_TAIL_RANK = 90
_TOPLEVEL_RANK = {
    "DataSources": 10,
    "DataSets": 11,
    "ReportSections": 12,
    "Body": 12,
    "ReportParameters": 13,
    "ReportParametersLayout": 14,
    "Variables": _TAIL_RANK,
    "Code": _TAIL_RANK,
    "EmbeddedImages": _TAIL_RANK,
    "CustomProperties": _TAIL_RANK,
    "ConsumeContainerWhitespace": _TAIL_RANK,
}


def _local(el) -> str:
    tag = el.tag
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _descendants(el, name: str) -> list:
    return [e for e in el.iter() if _local(e) == name]


def _kids(el, name: str) -> list:
    return [c for c in el if _local(c) == name] if el is not None else []


def _check_toplevel_order(root) -> list[str]:
    issues: list[str] = []
    prev_rank, prev_name = -1, ""
    for child in root:
        name = _local(child)
        rank = _TOPLEVEL_RANK.get(name)
        if rank is None:
            continue
        if rank < prev_rank:
            issues.append(
                f"Top-level <{name}> appears after <{prev_name}>; RDL requires "
                f"<{name}> first. Element order is load-bearing — the report "
                f"fails to load rather than reporting the problem."
            )
        prev_rank, prev_name = rank, name
    return issues


def _check_dimension_units(root) -> list[str]:
    issues: list[str] = []

    def walk(el) -> None:
        in_coord_space = _local(el) in _COORD_SPACE_TAGS
        for child in el:
            if _local(child) in _DIMENSION_TAGS:
                text = (child.text or "").strip()
                if text and not text.startswith("=") and not _DIMENSION_RE.match(text):
                    issues.append(
                        f"<{_local(child)}> is {text!r} — RDL sizes need a unit "
                        f"(in/cm/mm/pt/pc). 'px' is not valid RDL."
                    )
            if not in_coord_space:
                walk(child)

    walk(root)
    return issues


def _expression_owner(root, el) -> str:
    parents = {id(child): parent for parent in root.iter() for child in parent}
    node = el
    while node is not None:
        name = node.get("Name")
        if name:
            tag = node.tag.rsplit("}", 1)[-1]
            return f"The {tag} '{name}'"
        node = parents.get(id(node))
    return "An expression"


def _check_references(root) -> list[str]:
    issues: list[str] = []
    ds_names = {e.get("Name") for e in _descendants(root, "DataSource")}
    img_names = {e.get("Name") for e in _descendants(root, "EmbeddedImage")}

    for ds in _descendants(root, "DataSet"):
        for q in _kids(ds, "Query"):
            for src in _kids(q, "DataSourceName"):
                if (src.text or "").strip() not in ds_names:
                    issues.append(
                        f"DataSet '{ds.get('Name')}' references undefined "
                        f"DataSource '{(src.text or '').strip()}'."
                    )

    # Every Fields!X.Value must name a declared <Field>. Report Builder does not
    # resolve these until it renders, so an undeclared one is not a blank cell
    # but a hard "refers to the field 'X' ... can only refer to fields within
    # the current dataset scope", surfaced to whoever opens the report rather
    # than to whoever built it. The union across datasets is deliberate: with
    # more than one, which is in scope depends on the item, and a false refusal
    # here is worse than the narrower check.
    declared = {
        f.get("Name")
        for ds in _descendants(root, "DataSet")
        for f in _descendants(ds, "Field")
    }
    if declared:
        seen: set[tuple[str, str]] = set()
        for el in root.iter():
            for ref in re.findall(r"Fields!([0-9A-Za-z_]+)", el.text or ""):
                if ref in declared:
                    continue
                owner = _expression_owner(root, el)
                if (owner, ref) in seen:
                    continue
                seen.add((owner, ref))
                issues.append(
                    f"{owner} references field '{ref}', which no <Field> "
                    f"declares. Declared: {', '.join(sorted(declared)) or '(none)'}."
                )

    for img in _descendants(root, "Image"):
        source = next(iter(_kids(img, "Source")), None)
        if source is None or (source.text or "").strip() != "Embedded":
            continue
        value = next(iter(_kids(img, "Value")), None)
        val = (value.text or "").strip() if value is not None else ""
        # An expression picks the image at runtime; only a literal is checkable.
        if val and not val.startswith("=") and val not in img_names:
            issues.append(
                f"Image '{img.get('Name')}' is Embedded but its Value {val!r} "
                f"matches no <EmbeddedImage>."
            )

    return issues


def _leaf_members(container, ns) -> int:
    members = container.findall("r:TablixMember", ns)
    if not members:
        return 0
    total = 0
    for m in members:
        nested = m.find("r:TablixMembers", ns)
        total += _leaf_members(nested, ns) if nested is not None else 1
    return total


def _check_tablix_shape(t, ns) -> list[str]:
    issues: list[str] = []
    name = t.get("Name") or "?"
    body = t.find("r:TablixBody", ns)
    if body is None:
        return [f"Tablix '{name}' has no <TablixBody>."]

    rows = body.findall("r:TablixRows/r:TablixRow", ns)
    cols = body.findall("r:TablixColumns/r:TablixColumn", ns)

    row_h = t.find("r:TablixRowHierarchy/r:TablixMembers", ns)
    col_h = t.find("r:TablixColumnHierarchy/r:TablixMembers", ns)
    n_row_members = _leaf_members(row_h, ns) if row_h is not None else 0
    n_col_members = _leaf_members(col_h, ns) if col_h is not None else 0

    if n_row_members != len(rows):
        issues.append(
            f"Tablix '{name}': row hierarchy has {n_row_members} leaf member(s) "
            f"but TablixBody has {len(rows)} row(s). Report Builder throws "
            f"'index out of bounds' on this. Add or remove TablixMembers so the "
            f"counts match."
        )
    if n_col_members != len(cols):
        issues.append(
            f"Tablix '{name}': column hierarchy has {n_col_members} leaf "
            f"member(s) but TablixBody has {len(cols)} column(s). Report Builder "
            f"throws 'index out of bounds' on this."
        )
    # Cells-per-row == column count holds only for a FLAT tablix. A matrix
    # (column group) generates its columns at render time, and a row span
    # legitimately consumes cells from following rows — real reports do both.
    # Checking unconditionally would refuse to save valid matrix reports, and
    # pbi_paginated_set refuses on any issue. The leaf-count checks above still
    # apply to every tablix.
    col_hier_el = t.find("r:TablixColumnHierarchy", ns)
    has_column_group = col_hier_el is not None and any(
        _local(e) == "Group" for e in col_hier_el.iter()
    )
    has_row_span = any(
        (rs.text or "").strip() not in ("", "1") for rs in _descendants(t, "RowSpan")
    )
    if has_column_group or has_row_span:
        return issues

    for i, r in enumerate(rows):
        cells = r.findall("r:TablixCells/r:TablixCell", ns)
        if len(cells) != len(cols):
            issues.append(
                f"Tablix '{name}' row {i}: {len(cells)} cell(s) for "
                f"{len(cols)} column(s). Every row needs exactly one cell per "
                f"column."
            )
    return issues


def _check_names(root) -> list[str]:
    from collections import Counter

    issues: list[str] = []
    items: Counter = Counter()
    for tag in (
        "Textbox", "Tablix", "Chart", "Image", "Rectangle", "Line", "Subreport",
        "Gauge", "Map",
    ):
        for el in _descendants(root, tag):
            if el.get("Name"):
                items[el.get("Name")] += 1
    for name, count in sorted(items.items()):
        if count > 1:
            issues.append(
                f"Report item name '{name}' is used {count} times. Names are unique "
                "across the whole report, not per container; rename all but one."
            )

    for tag in ("DataSet", "DataSource", "ReportParameter", "EmbeddedImage"):
        counts = Counter(e.get("Name") for e in _descendants(root, tag) if e.get("Name"))
        for name, count in sorted(counts.items()):
            if count > 1:
                issues.append(f"{tag} '{name}' is declared {count} times.")

    for ds in _descendants(root, "DataSet"):
        counts = Counter(f.get("Name") for f in _descendants(ds, "Field") if f.get("Name"))
        for name, count in sorted(counts.items()):
            if count > 1:
                issues.append(
                    f"DataSet '{ds.get('Name')}' declares field '{name}' {count} times."
                )

    counts = Counter(g.get("Name") for g in _descendants(root, "Group") if g.get("Name"))
    for name, count in sorted(counts.items()):
        if count > 1:
            issues.append(
                f"Group name '{name}' is used {count} times; group names are unique "
                "across the report."
            )

    # Only a Textbox can be addressed as ReportItems!Name.
    boxes = {e.get("Name") for e in _descendants(root, "Textbox")}
    seen: set[str] = set()
    for el in root.iter():
        for ref in re.findall(r"ReportItems!([0-9A-Za-z_]+)", el.text or ""):
            if ref in boxes or ref in seen:
                continue
            seen.add(ref)
            issues.append(
                f"{_expression_owner(root, el)} references ReportItems!{ref}, but "
                f"no Textbox is named '{ref}'."
            )
    return issues


_DATA_REGIONS = frozenset({"Tablix", "Chart", "Gauge", "Map", "Subreport"})


def _check_scope(root, datasets: list) -> list[str]:
    if len(datasets) < 2:
        return []
    issues: list[str] = []
    parents = {id(child): parent for parent in root.iter() for child in parent}

    def in_region(el) -> bool:
        node = parents.get(id(el))
        while node is not None:
            if _local(node) in _DATA_REGIONS:
                return True
            node = parents.get(id(node))
        return False

    for tb in _descendants(root, "Textbox"):
        if in_region(tb):
            continue
        for v in _descendants(tb, "Value"):
            txt = v.text or ""
            if "Fields!" not in txt:
                continue
            scoped = re.findall(r'\w+\s*\(\s*Fields![^)]*?,\s*"([^"]+)"\s*\)', txt)
            if scoped:
                for scope in scoped:
                    if scope not in datasets:
                        issues.append(
                            f"Textbox '{tb.get('Name')}' aggregates over dataset "
                            f"'{scope}', which is not defined. Defined: "
                            f"{', '.join(d for d in datasets if d)}."
                        )
                continue
            issues.append(
                f"Textbox '{tb.get('Name')}' uses Fields! outside any data region, "
                f"and this report has {len(datasets)} datasets, so SSRS cannot tell "
                "which is meant. Wrap it in an aggregate with an explicit scope, "
                f'e.g. =First(Fields!X.Value, "{datasets[0]}").'
            )
    return issues


def _check_charts(root, datasets: list) -> list[str]:
    issues: list[str] = []
    for ch in _descendants(root, "Chart"):
        name = ch.get("Name") or "?"
        dsn = next(iter(_kids(ch, "DataSetName")), None)
        target = (dsn.text or "").strip() if dsn is not None else ""
        if not target:
            issues.append(f"Chart '{name}' has no <DataSetName>; a chart must bind a dataset.")
        elif target not in datasets:
            issues.append(f"Chart '{name}' references missing DataSet '{target}'.")
        if not _descendants(ch, "ChartData"):
            issues.append(f"Chart '{name}' has no <ChartData>; nothing would plot.")
    return issues


def _check_parameters(root, datasets: list) -> list[str]:
    issues: list[str] = []
    declared: set[str] = set()

    for p in _descendants(root, "ReportParameter"):
        name = p.get("Name") or "(unnamed)"
        declared.add(name)

        def flag(el, tag: str) -> bool:
            node = next(iter(_kids(el, tag)), None)
            return node is not None and (node.text or "").strip().lower() == "true"

        if flag(p, "MultiValue") and flag(p, "Nullable"):
            issues.append(
                f"Parameter '{name}' sets both MultiValue and Nullable. SSRS "
                "forbids that combination and rejects the whole report. Drop "
                "<Nullable>true</Nullable>, or set it to false — a multi-value "
                "parameter represents 'none selected' as an empty selection, "
                "not as null."
            )

        dt = next(iter(_kids(p, "DataType")), None)
        dtype = (dt.text or "").strip() if dt is not None else ""
        if not dtype:
            issues.append(f"Parameter '{name}' has no <DataType>; it is required.")
        elif flag(p, "MultiValue") and dtype.lower() == "boolean":
            issues.append(
                f"Parameter '{name}' is MultiValue with DataType Boolean; SSRS does "
                "not allow multi-value Boolean parameters."
            )

        # ValidValues/DefaultValue can be fed by a query; a missing dataset
        # here fails at load with the same unhelpful message.
        for holder in ("ValidValues", "DefaultValue"):
            for ref in _descendants(p, holder):
                for dsn in _descendants(ref, "DataSetName"):
                    target = (dsn.text or "").strip()
                    if target and target not in datasets:
                        issues.append(
                            f"Parameter '{name}' takes its {holder} from DataSet "
                            f"'{target}', which is not defined."
                        )

    # Parameters!X.Value in an expression must name a declared parameter --
    # the same failure shape as the Fields! check above.
    seen: set[str] = set()
    for el in root.iter():
        for ref in re.findall(r"Parameters!([0-9A-Za-z_]+)", el.text or ""):
            if ref in declared or ref in seen:
                continue
            seen.add(ref)
            issues.append(
                f"An expression references parameter '{ref}', which no "
                f"<ReportParameter> declares. Declared: "
                f"{', '.join(sorted(declared)) or '(none)'}."
            )
    return issues


_SCHEMA_PATH = Path(__file__).parent / "schemas" / "ReportDefinition2016.xsd"
_schema_cache: list = []


def _schema():
    if not _schema_cache:
        from lxml import etree

        _schema_cache.append(etree.XMLSchema(etree.parse(str(_SCHEMA_PATH))))
    return _schema_cache[0]


def validate_rdl_schema(xml_text: str) -> list[str]:
    try:
        from lxml import etree
    except ImportError:
        # Visible, not silent: a missing validator must not read as "valid".
        return ["Schema validation unavailable: the lxml package is not installed."]
    try:
        doc = etree.fromstring(xml_text.encode("utf-8"))
    except etree.XMLSyntaxError as e:
        return [f"Not well-formed XML: {e}"]
    schema = _schema()
    if schema.validate(doc):
        return []
    out: list[str] = []
    for err in list(schema.error_log)[:8]:
        msg = err.message
        # lxml prefixes every message with the namespaced element; the URI is
        # noise to a reader who knows they are looking at RDL.
        msg = msg.replace("{" + RDL_NS + "}", "")
        out.append(f"Schema (line {err.line}): {msg}")
    return out


def validate_rdl(xml_text: str) -> list[str]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        return [f"Not well-formed XML: {e}"]

    # Schema issues are appended after the hand-written checks, not returned in
    # their place: "Tablix 'T' row 2 has 3 cells for 4 columns" is what a
    # reader acts on, and the schema's "element X is not expected" for the same
    # fault is the corroboration. Any issue at all refuses the save, so the
    # order only decides what the reader sees first.

    ns = {"r": RDL_NS}
    issues: list[str] = []
    if root.tag != f"{{{RDL_NS}}}Report":
        issues.append(
            f"Root is '{root.tag}', expected Report in the 2016 RDL namespace."
        )
    if not root.findall(".//r:DataSource", ns):
        issues.append("No <DataSource> — the report isn't connected to a model.")
    datasets = [d.get("Name") for d in root.findall(".//r:DataSet", ns)]
    if not datasets:
        issues.append("No <DataSet> — no query to pull data.")
    for f in root.findall(".//r:Field", ns):
        nm = f.get("Name") or ""
        if nm[:1] == "_" or (nm[:1].isdigit()):
            issues.append(
                f"Field name '{nm}' is not CLS-compliant (no leading underscore/digit)."
            )
    tablixes = root.findall(".//r:Tablix", ns)
    if not tablixes:
        issues.append("No <Tablix> — nothing renders (need a table or matrix).")
    for t in tablixes:
        dsn = t.find("r:DataSetName", ns)
        name = dsn.text if dsn is not None else None
        if name not in datasets:
            issues.append(
                f"Tablix '{t.get('Name')}' references missing DataSet '{name}'."
            )
        issues.extend(_check_tablix_shape(t, ns))
    issues.extend(_check_toplevel_order(root))
    issues.extend(_check_dimension_units(root))
    issues.extend(_check_references(root))
    issues.extend(_check_parameters(root, datasets))
    issues.extend(_check_names(root))
    issues.extend(_check_scope(root, datasets))
    issues.extend(_check_charts(root, datasets))
    issues.extend(validate_rdl_schema(xml_text))
    if not root.findall(".//r:Page", ns):
        issues.append("No <Page> — page size/margins missing.")
    return issues


def unmatched_data_fields(xml_text: str, query_columns: list[str]) -> list[str]:
    def _leaf(name: str) -> str:
        n = (name or "").strip()
        if "[" in n and n.endswith("]"):
            n = n[n.rfind("[") + 1 : -1]
        return n.strip().casefold()

    have = {_leaf(c) for c in query_columns if (c or "").strip()}
    if not have:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    missing: list[str] = []
    for df in root.iter(f"{{{RDL_NS}}}DataField"):
        raw = (df.text or "").strip()
        if raw and _leaf(raw) not in have and raw not in missing:
            missing.append(raw)
    return missing
