from __future__ import annotations

import copy
import json
import os
import re

from config import (
    DESIGN_TOKENS,
    REPORT_DATA_COLORS,
    REPORT_LAYOUT,
    REPORT_PAGE_BG,
    SEMANTIC_BAD,
    SEMANTIC_GOOD,
    TINT_FACTORS,
)

THEME_NAME = "FabricBI_theme"

# Chrome colours for headers and navigation.
#
# These deliberately do NOT reuse `tableAccent`. That value is also
# dataColors[0], so defaulting banners and nav strips to it painted the whole
# report in one saturated blue -- bright bands competing with the data they
# sit above. Chrome should recede; accent belongs to the data.
HEADER_BG: str = DESIGN_TOKENS["header_bg"]
HEADER_FG: str = DESIGN_TOKENS["header_fg"]
NAV_SELECTED: str = DESIGN_TOKENS["header_bg"]
NAV_UNSELECTED: str = DESIGN_TOKENS["muted"]

# Report definition schema at or above which no legacy upgrade is needed.
MIN_MODERN_REPORT_SCHEMA: tuple[int, int, int] = (3, 2, 0)

_BG_IMAGE_LAYERS = ("canvas", "wallpaper")
_BG_IMAGE_SCALINGS = ("Fit", "Fill", "Tile", "Normal")

# Type scale for visual typography hierarchy
TYPE_SCALE: dict[str, int] = {
    "axis": 10,  # category / value axis tick labels
    "legend": 10,
    "data_label": 10,
    "body": 10,  # table + matrix cell values
    "header": 10,  # table + matrix column headers
    "kpi_label": 11,  # the quiet label above a KPI number
    "title": 14,  # visual titles -- the main structural step
    "page_title": 20,  # textClasses.largeTitle, for page banners
    "tooltip": 10,
}

# Elevation. A 95%-transparent shadow under a 1.13:1 border is, in practice,
# no elevation at all -- see _effective_hairline.
ELEVATION: dict[str, float] = {
    "shadow_transparency": 88,
    # Minimum contrast a card edge must reach against the card surface before
    # it counts as an edge rather than a rumour.
    "edge_min_contrast": 1.18,
    "edge_derive_tint": 0.85,  # tint of `muted` used when the token is too faint
}


def _tint(hex_color: str, factor: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    cr, cg, cb = (round(c + (255 - c) * factor) for c in (r, g, b))
    return f"#{cr:02X}{cg:02X}{cb:02X}"


def _mix(hex_a: str, hex_b: str, t: float) -> str:
    a = hex_a.lstrip("#")
    b = hex_b.lstrip("#")
    out = []
    for i in (0, 2, 4):
        ca, cb = int(a[i : i + 2], 16), int(b[i : i + 2], 16)
        out.append(round(ca + (cb - ca) * t))
    return "#{:02X}{:02X}{:02X}".format(*out)


def _rel_luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    chans = []
    for i in (0, 2, 4):
        c = int(h[i : i + 2], 16) / 255
        chans.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = chans
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(hex_a: str, hex_b: str) -> float:
    la, lb = _rel_luminance(hex_a), _rel_luminance(hex_b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _effective_hairline(hairline: str, card_bg: str, muted: str) -> str:
    try:
        if _contrast(hairline, card_bg) >= ELEVATION["edge_min_contrast"]:
            return hairline
        return _tint(muted, ELEVATION["edge_derive_tint"])
    except (ValueError, IndexError):
        return hairline


def _solid(color: str) -> dict:
    return {"solid": {"color": color}}


def _build_theme() -> dict:
    t = DESIGN_TOKENS
    ts = TYPE_SCALE
    accent = t["accent"]
    ink, muted, faint = t["ink"], t["muted"], t["faint"]
    card, grid = t["card_bg"], t["grid"]
    hairline = _effective_hairline(t["hairline"], card, muted)
    body, semi = t["font"], t["font_semibold"]
    radius = REPORT_LAYOUT["radius_px"]
    callout = REPORT_LAYOUT["callout_px"]
    pad = REPORT_LAYOUT["visual_padding_px"]
    rule = _tint(muted, 0.72)
    header_fill = _tint(grid, 0.5)
    zebra_fill = _tint(grid, 0.6)
    wallpaper = _mix(REPORT_PAGE_BG, ink, 0.06)

    axis = {
        "show": True,
        "showAxisTitle": False,
        "fontSize": ts["axis"],
        "fontFamily": body,
        "labelColor": _solid(muted),
        "labelDisplayUnits": 0,
    }
    value_axis = {**axis, "gridlineColor": _solid(grid), "gridlineThickness": 1}
    legend = [
        {
            "show": True,
            "position": "Bottom",
            "fontSize": ts["legend"],
            "fontFamily": body,
            "labelColor": _solid(muted),
        }
    ]
    data_labels = [
        {
            "show": True,
            "fontSize": ts["data_label"],
            "fontFamily": body,
            "color": _solid(ink),
            "labelDisplayUnits": 0,
        }
    ]
    card_surface = {
        "background": [{"show": True, "color": _solid(card), "transparency": 0}],
        "border": [
            {"show": True, "color": _solid(hairline), "radius": radius, "width": 1}
        ],
    }
    kpi_title = [
        {
            "show": True,
            "fontColor": _solid(muted),
            "fontSize": ts["kpi_label"],
            "fontFamily": semi,
            "alignment": "left",
        }
    ]
    kpi_value = {
        "fontColor": _solid(ink),
        "color": _solid(ink),
        "fontSize": callout,
        "fontFamily": semi,
        "labelDisplayUnits": 0,
    }
    chrome_off = {
        "background": [{"show": False}],
        "border": [{"show": False}],
        "dropShadow": [{"show": False}],
    }
    flat = {
        **chrome_off,
        "padding": [{"top": 0, "right": 0, "bottom": 0, "left": 0}],
    }
    slicer_style = {
        "padding": [{"top": pad, "right": pad, "bottom": pad, "left": pad}],
        "background": [{"show": True, "color": _solid(card), "transparency": 0}],
        "border": [
            {"show": True, "color": _solid(hairline), "radius": radius, "width": 1}
        ],
        "header": [
            {
                "show": True,
                "fontColor": _solid(muted),
                "fontSize": ts["header"],
                "fontFamily": semi,
            }
        ],
        "items": [
            {
                "fontColor": _solid(ink),
                "fontSize": ts["body"],
                "fontFamily": body,
                "background": _solid(card),
                "outline": "None",
            }
        ],
        "selection": [{"selectedColor": _solid(accent)}],
    }

    return {
        "name": THEME_NAME,
        "dataColors": list(REPORT_DATA_COLORS),
        "background": card,
        "foreground": ink,
        "tableAccent": accent,
        "good": SEMANTIC_GOOD,
        "neutral": faint,
        "bad": SEMANTIC_BAD,
        "minimum": _tint(accent, TINT_FACTORS["gradient_min"]),
        "center": _tint(accent, TINT_FACTORS["gradient_center"]),
        "maximum": accent,
        "null": _tint(muted, 0.88),
        "textClasses": {
            "largeTitle": {
                "fontSize": ts["page_title"],
                "fontFace": semi,
                "color": ink,
            },
            "title": {"fontSize": ts["title"], "fontFace": semi, "color": ink},
            "header": {"fontSize": ts["header"], "fontFace": semi, "color": ink},
            "label": {"fontSize": ts["body"], "fontFace": body, "color": muted},
            "callout": {"fontSize": callout, "fontFace": semi, "color": ink},
        },
        "visualStyles": {
            "*": {
                "*": {
                    "title": [
                        {
                            "show": True,
                            "fontColor": _solid(ink),
                            "fontSize": ts["title"],
                            "fontFamily": semi,
                            "alignment": "left",
                        }
                    ],
                    **copy.deepcopy(card_surface),
                    "visualHeader": [{"show": False}],
                    "dropShadow": [
                        {
                            "show": True,
                            "color": _solid(ink),
                            "transparency": ELEVATION["shadow_transparency"],
                            "position": "Outer",
                            "preset": "Bottom",
                        }
                    ],
                    "visualTooltip": [
                        {
                            "type": "Default",
                            "background": _solid(ink),
                            "titleFontColor": _solid(card),
                            "valueFontColor": _solid(_tint(ink, 0.85)),
                            "fontSize": ts["tooltip"],
                            "fontFamily": body,
                            "transparency": 4,
                        }
                    ],
                    "padding": [{"top": pad, "right": pad, "bottom": pad, "left": pad}],
                    "outspacePane": [
                        {
                            "backgroundColor": _solid(card),
                            "foregroundColor": _solid(ink),
                            "borderColor": _solid(hairline),
                            "border": True,
                            "transparency": 0,
                            "titleSize": ts["title"],
                            "headerSize": ts["kpi_label"],
                            "fontFamily": body,
                            "checkboxAndApplyColor": _solid(accent),
                            "inputBoxColor": _solid(card),
                        }
                    ],
                    "filterCard": [
                        {
                            "$id": "Applied",
                            "backgroundColor": _solid(_tint(accent, 0.92)),
                            "borderColor": _solid(_tint(accent, 0.7)),
                            "foregroundColor": _solid(ink),
                            "inputBoxColor": _solid(card),
                            "textSize": ts["body"],
                            "fontFamily": body,
                            "border": True,
                            "transparency": 0,
                        },
                        {
                            "$id": "Available",
                            "backgroundColor": _solid(card),
                            "borderColor": _solid(hairline),
                            "foregroundColor": _solid(muted),
                            "inputBoxColor": _solid(card),
                            "textSize": ts["body"],
                            "fontFamily": body,
                            "border": True,
                            "transparency": 0,
                        },
                    ],
                }
            },
            "image": {"*": copy.deepcopy(flat)},
            "shape": {"*": copy.deepcopy(flat)},
            "textbox": {"*": copy.deepcopy(chrome_off)},
            "pageNavigator": {"*": copy.deepcopy(flat)},
            "actionButton": {"*": copy.deepcopy(flat)},
            "cardVisual": {
                "*": {
                    "title": copy.deepcopy(kpi_title),
                    "value": [{**kpi_value, "horizontalAlignment": "left"}],
                    "label": [{"show": False, "visible": False}],
                    "cards": [{"showLabel": False}],
                    "outline": [{"show": False}],
                    "shapeCustomRectangle": [{"show": False}],
                    **copy.deepcopy(card_surface),
                }
            },
            "card": {
                "*": {
                    "title": copy.deepcopy(kpi_title),
                    "categoryLabels": [{"show": False}],
                    "labels": [dict(kpi_value)],
                    "wordWrap": [{"show": False}],
                    **copy.deepcopy(card_surface),
                }
            },
            "multiRowCard": {
                "*": {
                    "cardTitle": [
                        {
                            "fontColor": _solid(ink),
                            "fontSize": ts["header"],
                            "fontFamily": semi,
                        }
                    ],
                    "dataLabels": [
                        {
                            "color": _solid(ink),
                            "fontSize": ts["title"],
                            "fontFamily": semi,
                        }
                    ],
                    "categoryLabels": [
                        {
                            "show": True,
                            "color": _solid(muted),
                            "fontSize": ts["body"],
                            "fontFamily": body,
                        }
                    ],
                    "card": [{"outlineColor": _solid(hairline), "outlineWeight": 1}],
                    **copy.deepcopy(card_surface),
                }
            },
            "kpi": {
                "*": {
                    "indicator": [
                        {
                            "fontSize": callout,
                            "fontFamily": semi,
                            "color": _solid(ink),
                        }
                    ],
                    "trendline": [{"show": True, "color": _solid(_tint(accent, 0.55))}],
                    "goals": [
                        {
                            "show": True,
                            "color": _solid(muted),
                            "fontSize": ts["body"],
                            "fontFamily": body,
                        }
                    ],
                    "status": [
                        {
                            "goodColor": _solid(SEMANTIC_GOOD),
                            "badColor": _solid(SEMANTIC_BAD),
                        }
                    ],
                    **copy.deepcopy(card_surface),
                }
            },
            "gauge": {
                "*": {
                    "calloutValue": [
                        {
                            "fontSize": callout,
                            "fontFamily": semi,
                            "color": _solid(ink),
                            "labelDisplayUnits": 0,
                        }
                    ],
                    "labels": [
                        {
                            "show": True,
                            "color": _solid(muted),
                            "fontSize": ts["axis"],
                            "fontFamily": body,
                        }
                    ],
                    "targetLabel": [
                        {"show": True, "color": _solid(muted), "fontSize": ts["axis"]}
                    ],
                    "axis": [{"targetColor": _solid(ink)}],
                    **copy.deepcopy(card_surface),
                }
            },
            "barChart": {
                "*": {
                    "legend": copy.deepcopy(legend),
                    "valueAxis": [{**value_axis, "show": False}],
                    "categoryAxis": [dict(axis)],
                    "labels": [{**data_labels[0], "labelPosition": "OutsideEnd"}],
                    "plotArea": [{"transparency": 0}],
                }
            },
            "columnChart": {
                "*": {
                    "legend": copy.deepcopy(legend),
                    "valueAxis": [{**value_axis, "show": False}],
                    "categoryAxis": [dict(axis)],
                    "labels": [{**data_labels[0], "labelPosition": "OutsideEnd"}],
                }
            },
            "lineChart": {
                "*": {
                    "lineStyles": [
                        {
                            "strokeWidth": 2,
                            "showMarker": False,
                            "markerShape": "circle",
                            "markerSize": 4,
                        }
                    ],
                    "legend": copy.deepcopy(legend),
                    "valueAxis": [dict(value_axis)],
                    "categoryAxis": [dict(axis)],
                }
            },
            "areaChart": {
                "*": {
                    "lineStyles": [
                        {
                            "strokeWidth": 2,
                            "showMarker": False,
                        }
                    ],
                    "legend": copy.deepcopy(legend),
                    "valueAxis": [dict(value_axis)],
                    "categoryAxis": [dict(axis)],
                    "plotArea": [{"transparency": 35}],
                }
            },
            "lineClusteredColumnComboChart": {
                "*": {
                    "legend": copy.deepcopy(legend),
                    "valueAxis": [dict(value_axis)],
                    "y2Axis": [{**value_axis, "gridlineShow": False}],
                    "categoryAxis": [dict(axis)],
                    "lineStyles": [
                        {"strokeWidth": 2, "showMarker": False, "markerSize": 4}
                    ],
                    "labels": [{**data_labels[0], "show": False}],
                }
            },
            "scatterChart": {
                "*": {
                    "legend": copy.deepcopy(legend),
                    "valueAxis": [{**value_axis, "showAxisTitle": True}],
                    "categoryAxis": [{**axis, "showAxisTitle": True}],
                    "fillPoint": [{"show": True}],
                    "bubbles": [{"bubbleSize": 4}],
                }
            },
            "waterfallChart": {
                "*": {
                    "legend": copy.deepcopy(legend),
                    "valueAxis": [dict(value_axis)],
                    "categoryAxis": [dict(axis)],
                    "labels": [dict(data_labels[0])],
                    "sentimentColors": [
                        {
                            "increaseFill": _solid(SEMANTIC_GOOD),
                            "decreaseFill": _solid(SEMANTIC_BAD),
                            "totalFill": _solid(muted),
                            "otherFill": _solid(faint),
                        }
                    ],
                }
            },
            "funnel": {
                "*": {
                    "labels": [dict(data_labels[0])],
                    "categoryLabels": [
                        {
                            "show": True,
                            "color": _solid(muted),
                            "fontSize": ts["axis"],
                            "fontFamily": body,
                        }
                    ],
                    "percentBarLabel": [
                        {
                            "show": True,
                            "color": _solid(muted),
                            "fontSize": ts["axis"],
                        }
                    ],
                }
            },
            "treemap": {
                "*": {
                    "legend": [{**legend[0], "show": False}],
                    "labels": [dict(data_labels[0])],
                    "categoryLabels": [
                        {
                            "show": True,
                            "color": _solid(card),
                            "fontSize": ts["body"],
                            "fontFamily": semi,
                        }
                    ],
                }
            },
            "ribbonChart": {
                "*": {
                    "legend": copy.deepcopy(legend),
                    "valueAxis": [{**value_axis, "show": False}],
                    "categoryAxis": [dict(axis)],
                    "labels": [dict(data_labels[0])],
                    "ribbonBands": [
                        {"spacing": 4, "matchSeriesColor": True, "transparency": 40}
                    ],
                }
            },
            "donutChart": {
                "*": {
                    "legend": [{**legend[0], "position": "Bottom"}],
                    "labels": [
                        {
                            "show": True,
                            "color": _solid(ink),
                            "fontSize": ts["data_label"],
                            "fontFamily": body,
                            "labelStyle": "Percent of total",
                            "position": "Outside",
                        }
                    ],
                    "slices": [{"innerRadiusRatio": 65}],
                }
            },
            "pieChart": {
                "*": {
                    "legend": [{**legend[0], "position": "Bottom"}],
                    "labels": [
                        {
                            "show": True,
                            "color": _solid(ink),
                            "fontSize": ts["data_label"],
                            "fontFamily": body,
                            "position": "Outside",
                        }
                    ],
                }
            },
            "slicer": {"*": copy.deepcopy(slicer_style)},
            "tableEx": {
                "*": {
                    "grid": [
                        {
                            "gridVertical": False,
                            "gridHorizontal": True,
                            "gridHorizontalColor": _solid(grid),
                            "gridHorizontalWeight": 1,
                            "rowPadding": 5,
                        }
                    ],
                    "columnHeaders": [
                        {
                            "autoSizeColumnWidth": True,
                            "columnAdjustment": "growToFit",
                            "fontColor": _solid(muted),
                            "backColor": _solid(header_fill),
                            "fontSize": ts["header"],
                            "fontFamily": semi,
                            "alignment": "Left",
                            "outline": "BottomOnly",
                            "outlineColor": _solid(rule),
                            "outlineWeight": 1,
                        }
                    ],
                    "values": [
                        {
                            "fontColorPrimary": _solid(ink),
                            "fontColorSecondary": _solid(ink),
                            "backColorPrimary": _solid(card),
                            "backColorSecondary": _solid(zebra_fill),
                            "fontSize": ts["body"],
                            "fontFamily": body,
                        }
                    ],
                    "total": [{"totals": False}],
                }
            },
            "pivotTable": {
                "*": {
                    "grid": [
                        {
                            "gridVertical": True,
                            "gridVerticalColor": _solid(rule),
                            "gridVerticalWeight": 1,
                            "gridHorizontal": True,
                            "gridHorizontalColor": _solid(rule),
                            "gridHorizontalWeight": 1,
                            "rowPadding": 6,
                        }
                    ],
                    "columnHeaders": [
                        {
                            "autoSizeColumnWidth": True,
                            "columnAdjustment": "growToFit",
                            "fontColor": _solid(muted),
                            "backColor": _solid(header_fill),
                            "fontSize": ts["header"],
                            "fontFamily": semi,
                        }
                    ],
                    "rowHeaders": [
                        {
                            "fontColor": _solid(ink),
                            "fontSize": ts["body"],
                            "fontFamily": semi,
                            "backColor": _solid(card),
                        }
                    ],
                    "subTotals": [
                        {
                            "fontColor": _solid(ink),
                            "backColor": _solid(header_fill),
                            "fontFamily": semi,
                            "applyToHeaders": True,
                        }
                    ],
                    "values": [
                        {
                            "fontColorPrimary": _solid(ink),
                            "fontColorSecondary": _solid(ink),
                            "backColorPrimary": _solid(card),
                            "backColorSecondary": _solid(grid),
                            "bandedRowHeaders": True,
                            "fontSize": ts["body"],
                            "fontFamily": body,
                        }
                    ],
                }
            },
            "decompositionTreeVisual": {
                "*": {
                    "levelHeaders": [
                        {
                            "fontColor": _solid(muted),
                            "fontSize": ts["header"],
                            "fontFamily": semi,
                        }
                    ],
                    "dataBars": [{"show": True, "positiveBarColor": _solid(accent)}],
                    "labels": [
                        {
                            "color": _solid(ink),
                            "fontSize": ts["body"],
                            "fontFamily": body,
                        }
                    ],
                }
            },
            "keyInfluencersVisual": {
                "*": {
                    "influencerNodeStyle": [{"fill": _solid(card)}],
                    "bubbleStyle": [{"fill": _solid(accent)}],
                }
            },
            "map": {
                "*": {
                    "mapStyles": [{"mapTheme": "canvasLight"}],
                    "bubbles": [{"bubbleSize": -15}],
                    "legend": copy.deepcopy(legend),
                }
            },
            "filledMap": {
                "*": {
                    "mapStyles": [{"mapTheme": "canvasLight"}],
                    "legend": copy.deepcopy(legend),
                }
            },
            "shapeMap": {
                "*": {
                    "default": [{"fill": _solid(grid), "stroke": _solid(hairline)}],
                    "legend": copy.deepcopy(legend),
                }
            },
            "azureMap": {
                "*": {
                    "mapControls": [{"defaultStyle": "road"}],
                }
            },
            "page": {
                "*": {
                    "background": [
                        {"color": _solid(REPORT_PAGE_BG), "transparency": 0}
                    ],
                    "outspace": [{"color": _solid(wallpaper), "transparency": 0}],
                }
            },
        },
    }


THEME_JSON: dict = _build_theme()


def _clone_style(src: str, dst: str, *, drop_label_position: bool = False) -> None:
    if src not in THEME_JSON["visualStyles"]:
        return
    style = copy.deepcopy(THEME_JSON["visualStyles"][src])
    if drop_label_position:
        try:
            style["*"]["labels"][0].pop("labelPosition", None)
        except (KeyError, IndexError, TypeError):
            pass
    THEME_JSON["visualStyles"].setdefault(dst, style)


# Power BI visual type style aliasing
_STACKED = {
    "stackedBarChart",
    "stackedColumnChart",
    "hundredPercentStackedBarChart",
    "hundredPercentStackedColumnChart",
}
for _src, _dsts in (
    (
        "barChart",
        ("clusteredBarChart", "stackedBarChart", "hundredPercentStackedBarChart"),
    ),
    (
        "columnChart",
        (
            "clusteredColumnChart",
            "stackedColumnChart",
            "hundredPercentStackedColumnChart",
        ),
    ),
    ("areaChart", ("stackedAreaChart", "hundredPercentStackedAreaChart")),
    ("lineClusteredColumnComboChart", ("lineStackedColumnComboChart",)),
    ("kpi", ("kpiNew",)),
    # Every slicer variant shares the slicer chrome. advancedSlicerVisual is
    # the newer button/list slicer that Desktop now inserts by default.
    ("slicer", ("listSlicer", "textSlicer", "advancedSlicerVisual")),
    ("pageNavigator", ("bookmarkNavigator", "personalizeVisual")),
):
    for _dst in _dsts:
        _clone_style(_src, _dst, drop_label_position=_dst in _STACKED)


def _follow_accent(theme: dict, accent: str) -> None:
    try:
        base = theme["visualStyles"]["*"]["*"]
        base["outspacePane"][0]["checkboxAndApplyColor"] = _solid(accent)
        applied = next(
            (c for c in base["filterCard"] if c.get("$id") == "Applied"), None
        )
        if applied:
            applied["backgroundColor"] = _solid(_tint(accent, 0.92))
            applied["borderColor"] = _solid(_tint(accent, 0.7))
    except (KeyError, IndexError, TypeError):
        pass
    for _name in ("slicer", "listSlicer", "textSlicer", "advancedSlicerVisual"):
        try:
            theme["visualStyles"][_name]["*"]["selection"][0]["selectedColor"] = _solid(
                accent
            )
        except (KeyError, IndexError, TypeError):
            continue
    try:
        theme["visualStyles"]["decompositionTreeVisual"]["*"]["dataBars"][0][
            "positiveBarColor"
        ] = _solid(accent)
    except (KeyError, IndexError, TypeError):
        pass


def extract_palette(markup: str, max_colors: int = 8) -> list[str]:
    if not markup:
        return []
    hexes = re.findall(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b", markup)
    rgbs = re.findall(r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})", markup)

    def _norm(h: str) -> str:
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return "#" + h.lower()

    counts: dict[str, int] = {}
    order: list[str] = []
    for h in hexes:
        c = _norm(h)
        if c not in counts:
            order.append(c)
        counts[c] = counts.get(c, 0) + 1
    for r, g, b in rgbs:
        try:
            c = f"#{int(r):02x}{int(g):02x}{int(b):02x}"
        except ValueError:
            continue
        if c not in counts:
            order.append(c)
        counts[c] = counts.get(c, 0) + 1

    def _keep(c: str) -> bool:
        r, g, b = int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)
        if min(r, g, b) > 0xE0:
            return False
        if max(r, g, b) < 0x20:
            return False
        return max(r, g, b) - min(r, g, b) >= 24

    palette = sorted((c for c in order if _keep(c)), key=lambda c: -counts[c])
    return palette[:max_colors] if len(palette) >= 3 else []


def _backup_file(path: str) -> str | None:
    if not os.path.isfile(path):
        return None
    try:
        import shutil
        from datetime import datetime, timezone

        bdir = os.path.join(os.path.dirname(path), ".backups")
        os.makedirs(bdir, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        dest = os.path.join(bdir, f"{os.path.basename(path)}.{stamp}.bak")
        shutil.copy2(path, dest)
        return dest
    except Exception:
        return None


def _upsert_resource_package(rpt: dict) -> dict:
    packages = rpt.setdefault("resourcePackages", [])
    pkg = next((p for p in packages if p.get("name") == "RegisteredResources"), None)
    if pkg is None:
        pkg = {
            "name": "RegisteredResources",
            "type": "RegisteredResources",
            "items": [],
        }
        packages.append(pkg)
    return pkg


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _dedupe_hexes(colors: list[str], limit: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for c in colors:
        key = c.lower() if isinstance(c, str) else repr(c)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
        if len(out) >= limit:
            break
    return out


def _report_schema_version(url: str) -> tuple[int, int, int] | None:
    m = re.search(r"/report/(\d+)\.(\d+)\.(\d+)/", url or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _theme_with_palette(
    palette: list[str] | None,
    *,
    accent: str | None = None,
    page_bg: str | None = None,
    good: str | None = None,
    bad: str | None = None,
    base: dict | None = None,
) -> dict:
    if not (palette or accent or page_bg or good or bad):
        # Always a copy: returning the module-level THEME_JSON let a caller
        # mutate the shared default and poison every later injection in the
        # same process.
        return copy.deepcopy(base if base is not None else THEME_JSON)
    theme = copy.deepcopy(THEME_JSON)
    if base:
        theme = _deep_merge(theme, base)
    if palette:
        theme["dataColors"] = _dedupe_hexes(
            list(palette) + (theme.get("dataColors") or THEME_JSON["dataColors"]),
            8,
        )
    accent = accent or (
        palette[0]
        if palette
        else theme.get("tableAccent") or THEME_JSON["dataColors"][0]
    )
    theme["tableAccent"] = accent
    theme["maximum"] = accent
    theme["minimum"] = _tint(accent, TINT_FACTORS["gradient_min"])
    theme["center"] = _tint(accent, TINT_FACTORS["gradient_center"])
    _follow_accent(theme, accent)
    if good:
        theme["good"] = good
    if bad:
        theme["bad"] = bad
    if page_bg:
        try:
            page_style = theme["visualStyles"]["page"]["*"]
            page_style["background"][0]["color"]["solid"]["color"] = page_bg
            # The wallpaper is derived from the page, so it has to be
            # re-derived here or a client page_bg leaves the frame around the
            # canvas at the default deployment's shade.
            page_style["outspace"][0]["color"]["solid"]["color"] = _mix(
                page_bg, DESIGN_TOKENS["ink"], 0.06
            )
        except (KeyError, IndexError, TypeError):
            pass
    # KPI numbers deliberately keep their ink colour when a client palette
    # arrives -- accent belongs to marks and gradients, not to every headline
    # figure on the page.
    return theme


def inject_theme(
    report_folder: str,
    palette: list[str] | None = None,
    *,
    accent: str | None = None,
    page_bg: str | None = None,
    good: str | None = None,
    bad: str | None = None,
) -> str:
    if not os.path.isdir(report_folder):
        return f"[polish] skip — report folder not found: {report_folder}"

    static_dir = os.path.join(report_folder, "StaticResources", "RegisteredResources")
    os.makedirs(static_dir, exist_ok=True)
    theme_path = os.path.join(static_dir, f"{THEME_NAME}.json")
    existing_theme: dict | None = None
    if os.path.isfile(theme_path):
        try:
            with open(theme_path, encoding="utf-8") as f:
                existing_theme = json.load(f)
        except Exception:
            existing_theme = None
    theme = _theme_with_palette(
        palette, accent=accent, page_bg=page_bg, good=good, bad=bad, base=existing_theme
    )
    _backup_file(theme_path)
    with open(theme_path, "w", encoding="utf-8") as f:
        json.dump(theme, f, indent=2)

    version_path = os.path.join(report_folder, "definition", "version.json")
    if os.path.isfile(version_path):
        with open(version_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/versionMetadata/1.0.0/schema.json",
                    "version": "2.0.0",
                },
                f,
                indent=2,
            )

    report_json_path = os.path.join(report_folder, "definition", "report.json")
    if os.path.isfile(report_json_path):
        with open(report_json_path, encoding="utf-8") as f:
            rpt = json.load(f)
        cur_ver = _report_schema_version(rpt.get("$schema", ""))
        # An unparseable/absent $schema is treated as legacy on purpose: PBIR
        # always writes one, so its absence means a hand-edited or pre-PBIR
        # file that needs the same upgrade.
        is_legacy = cur_ver is None or cur_ver < MIN_MODERN_REPORT_SCHEMA
        if is_legacy:
            rpt["$schema"] = (
                "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/report/3.2.0/schema.json"
            )
            rpt.pop("layoutOptimization", None)
        existing_base = (rpt.get("themeCollection") or {}).get("baseTheme") or {}
        version_tuple = existing_base.get("reportVersionAtImport")
        if not isinstance(version_tuple, dict):
            version_tuple = {
                "visual": "1.8.91",
                "report": "2.0.91",
                "page": "1.3.91",
            }
        rpt.setdefault("themeCollection", {})
        rpt["themeCollection"]["baseTheme"] = {
            "name": existing_base.get("name") or "CY24SU06",
            "reportVersionAtImport": version_tuple,
            "type": existing_base.get("type") or "SharedResources",
        }
        rpt["themeCollection"]["customTheme"] = {
            "name": f"{THEME_NAME}.json",
            "reportVersionAtImport": version_tuple,
            "type": "RegisteredResources",
        }
        rpt.setdefault("objects", {}).setdefault(
            "outspacePane",
            [{"properties": {"expanded": {"expr": {"Literal": {"Value": "true"}}}}}],
        )
        _settings_defaults = {
            "useStylableVisualContainerHeader": True,
            "defaultDrillFilterOtherVisuals": True,
            "allowChangeFilterTypes": True,
            "useEnhancedTooltips": True,
            "useDefaultAggregateDisplayName": True,
        }
        rpt_settings = rpt.setdefault("settings", {})
        for _k, _v in _settings_defaults.items():
            rpt_settings.setdefault(_k, _v)
        pkg = _upsert_resource_package(rpt)
        items = pkg.setdefault("items", [])
        _tn = f"{THEME_NAME}.json"
        theme_item = next(
            (i for i in items if i.get("name") in (THEME_NAME, _tn)), None
        )
        if theme_item is None:
            theme_item = {"type": "CustomTheme"}
            items.append(theme_item)
        theme_item["name"] = _tn
        theme_item["path"] = _tn
        _backup_file(report_json_path)
        with open(report_json_path, "w", encoding="utf-8") as f:
            json.dump(rpt, f, indent=2)

    return f"[polish] theme injected → {theme_path}"


def set_page_background_image(
    report_folder: str,
    page: str,
    image_path: str,
    *,
    scaling: str = "Fit",
    layer: str = "canvas",
) -> str:
    import glob as _glob

    if not os.path.isdir(report_folder):
        return f"[bg-image] ERROR — report folder not found: {report_folder}"
    if not os.path.isfile(image_path):
        return f"[bg-image] ERROR — image not found: {image_path}"
    if scaling not in _BG_IMAGE_SCALINGS:
        return (
            f"[bg-image] ERROR — scaling must be Fit/Fill/Tile/Normal, got {scaling!r}"
        )
    # Validated rather than inferred: previously anything that was not
    # "wallpaper" silently fell through to the canvas layer, so a typo like
    # "wallpapper" painted the wrong surface with no error.
    if layer not in _BG_IMAGE_LAYERS:
        return f"[bg-image] ERROR — layer must be canvas/wallpaper, got {layer!r}"
    obj_key = "outspace" if layer == "wallpaper" else "background"

    item_name = _register_image_resource(report_folder, image_path)

    page_json = os.path.join(report_folder, "definition", "pages", page, "page.json")
    if not os.path.isfile(page_json):
        hits = _glob.glob(f"{report_folder}/**/pages/{page}/page.json", recursive=True)
        page_json = hits[0] if hits else None
    if not page_json or not os.path.isfile(page_json):
        return f"[bg-image] ERROR — page '{page}' not found under {report_folder}"
    with open(page_json, encoding="utf-8") as f:
        pd = json.load(f)
    objects = pd.setdefault("objects", {})
    arr = objects.setdefault(obj_key, [{"properties": {}}])
    if not arr:
        arr.append({"properties": {}})
    props = arr[0].setdefault("properties", {})
    props["image"] = {
        "image": {
            "name": _lit(f"'{item_name}'"),
            "url": {
                "expr": {
                    "ResourcePackageItem": {
                        "PackageName": "RegisteredResources",
                        "PackageType": 1,
                        "ItemName": item_name,
                    }
                }
            },
            "scaling": _lit(f"'{scaling}'"),
        }
    }
    _backup_file(page_json)
    with open(page_json, "w", encoding="utf-8") as f:
        json.dump(pd, f, indent=2)
    return (
        f"[bg-image] {page}: {obj_key} ← {item_name} (scaling={scaling}). "
        f"Reload Desktop to confirm it renders."
    )


def wire_drillthrough_page(report_folder: str, page: str, field: str) -> str:
    import glob as _glob
    import secrets as _secrets

    if not os.path.isdir(report_folder):
        return f"[drillthrough] ERROR — report folder not found: {report_folder}"
    if "[" not in field or not field.rstrip().endswith("]"):
        return (
            "[drillthrough] ERROR — field must be 'Table[Column]' "
            f"(e.g. Sales_Fact[Region]); got {field!r}"
        )
    entity = field.split("[", 1)[0].strip().strip("'").strip('"')
    column = field.split("[", 1)[1].rstrip("]").strip()
    if not entity or not column:
        return (
            "[drillthrough] ERROR — field must be 'Table[Column]' "
            f"(e.g. Sales_Fact[Region]); got {field!r}"
        )

    page_json = os.path.join(report_folder, "definition", "pages", page, "page.json")
    if not os.path.isfile(page_json):
        hits = _glob.glob(f"{report_folder}/**/pages/{page}/page.json", recursive=True)
        page_json = hits[0] if hits else None
    if not page_json or not os.path.isfile(page_json):
        return (
            f"[drillthrough] ERROR — target page '{page}' not found under "
            f"{report_folder}. Create it with pbi_report_add_page and add at "
            f"least one visual, then retry."
        )

    def _col_ref() -> dict:
        return {
            "Column": {
                "Expression": {"SourceRef": {"Entity": entity}},
                "Property": column,
            }
        }

    with open(page_json, encoding="utf-8") as f:
        pd = json.load(f)

    filter_name = _secrets.token_hex(10)
    pd["pageBinding"] = {
        "name": _secrets.token_hex(10),
        "type": "Drillthrough",
        "parameters": [
            {
                "name": _secrets.token_hex(10),
                "boundFilter": filter_name,
                "fieldExpr": _col_ref(),
            }
        ],
    }
    fc = pd.setdefault("filterConfig", {})
    kept = [
        f for f in (fc.get("filters") or []) if f.get("howCreated") != "Drillthrough"
    ]
    kept.append(
        {
            "name": filter_name,
            "field": _col_ref(),
            "type": "Categorical",
            "howCreated": "Drillthrough",
        }
    )
    fc["filters"] = kept
    pd["visibility"] = "HiddenInViewMode"

    _backup_file(page_json)
    with open(page_json, "w", encoding="utf-8") as f:
        json.dump(pd, f, indent=2)

    msg = (
        f"[drillthrough] page '{page}' is now a drillthrough target on "
        f"{entity}[{column}] (hidden from the tab strip). No source visual was "
        f"changed — right-click any visual that uses {entity}[{column}] and "
        f'choose "Drill through". Run pbi_report_health_check, then reload '
        f"Desktop to confirm."
    )
    n_visuals = len(
        _glob.glob(
            os.path.join(os.path.dirname(page_json), "visuals", "*", "visual.json")
        )
    )
    if not n_visuals:
        msg += (
            f"\n[drillthrough] WARNING — page '{page}' has 0 visuals, so "
            f"pbi_report_health_check will fail (report_not_empty). Add the "
            f"detail visuals with pbi_visual_add_bound."
        )
    return msg


def _lit(val: str) -> dict:
    return {"expr": {"Literal": {"Value": val}}}


def _register_image_resource(report_folder: str, image_path: str) -> str:
    import shutil

    static_dir = os.path.join(report_folder, "StaticResources", "RegisteredResources")
    os.makedirs(static_dir, exist_ok=True)
    item_name = os.path.basename(image_path)
    dest = os.path.join(static_dir, item_name)
    if os.path.abspath(dest) != os.path.abspath(image_path):
        shutil.copy2(image_path, dest)

    report_json_path = os.path.join(report_folder, "definition", "report.json")
    if os.path.isfile(report_json_path):
        with open(report_json_path, encoding="utf-8") as f:
            rpt = json.load(f)
        pkg = _upsert_resource_package(rpt)
        items = pkg.setdefault("items", [])
        if not any(i.get("name") == item_name for i in items):
            items.append({"name": item_name, "path": item_name, "type": "Image"})
        _backup_file(report_json_path)
        with open(report_json_path, "w", encoding="utf-8") as f:
            json.dump(rpt, f, indent=2)
    return item_name


def add_image_visual(
    report_folder: str,
    page: str,
    image_path: str,
    visual_name: str,
    x: int,
    y: int,
    width: int,
    height: int,
    *,
    scaling: str = "Fit",
) -> str:
    import glob as _glob

    if not os.path.isdir(report_folder):
        return f"[image-visual] ERROR — report folder not found: {report_folder}"
    if not os.path.isfile(image_path):
        return f"[image-visual] ERROR — image not found: {image_path}"
    if scaling not in ("Fit", "Fill", "Normal"):
        return (
            f"[image-visual] ERROR — scaling must be Fit/Fill/Normal, got {scaling!r}"
        )

    page_dir = os.path.join(report_folder, "definition", "pages", page)
    if not os.path.isdir(page_dir):
        hits = _glob.glob(f"{report_folder}/**/pages/{page}/page.json", recursive=True)
        if not hits:
            return (
                f"[image-visual] ERROR — page '{page}' not found under {report_folder}"
            )
        page_dir = os.path.dirname(hits[0])

    item_name = _register_image_resource(report_folder, image_path)

    visual_dir = os.path.join(page_dir, "visuals", visual_name)
    os.makedirs(visual_dir, exist_ok=True)
    vis = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.10.0/schema.json",
        "name": visual_name,
        "position": {
            "x": x,
            "y": y,
            "z": 1000,
            "height": height,
            "width": width,
            "tabOrder": 1000,
        },
        "visual": {
            "visualType": "image",
            "objects": {
                "general": [
                    {
                        "properties": {
                            "imageUrl": {
                                "expr": {
                                    "ResourcePackageItem": {
                                        "PackageName": "RegisteredResources",
                                        "PackageType": 1,
                                        "ItemName": item_name,
                                    }
                                }
                            },
                            "imageScalingType": _lit(f"'{scaling}'"),
                        }
                    }
                ]
            },
            "drillFilterOtherVisuals": True,
        },
    }
    with open(os.path.join(visual_dir, "visual.json"), "w", encoding="utf-8") as f:
        json.dump(vis, f, indent=2)
    return (
        f"[image-visual] {page}/{visual_name}: {item_name} placed at "
        f"({x},{y}) {width}x{height} (scaling={scaling})."
    )