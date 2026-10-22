
from __future__ import annotations

import math
from typing import Any

from config import REPORT_LAYOUT

VisualClass = str  # one of _VALID_CLASSES

_VALID_CLASSES = frozenset(
    {"header", "slicer", "kpi", "chart", "table", "matrix", "supporting"}
)

SLICER_VISUAL_TYPES = frozenset(
    {"slicer", "listSlicer", "textSlicer", "advancedSlicerVisual"}
)
CARD_VISUAL_TYPES = frozenset({"card", "cardVisual", "multiRowCard", "kpi", "kpiNew"})
TABLE_VISUAL_TYPES = frozenset({"tableEx"})
MATRIX_VISUAL_TYPES = frozenset({"pivotTable"})
DECORATIVE_VISUAL_TYPES = frozenset(
    {"textbox", "image", "shape", "actionButton", "pageNavigator", ""}
)
BAR_CHART_TYPES = frozenset(
    {
        "barChart",
        "clusteredBarChart",
        "columnChart",
        "clusteredColumnChart",
        "stackedBarChart",
        "stackedColumnChart",
        "hundredPercentStackedBarChart",
        "hundredPercentStackedColumnChart",
    }
)
COMBO_CHART_TYPES = frozenset(
    {"lineStackedColumnComboChart", "lineClusteredColumnComboChart"}
)
CARTESIAN_CHART_TYPES = (
    BAR_CHART_TYPES
    | COMBO_CHART_TYPES
    | frozenset({"lineChart", "areaChart", "stackedAreaChart"})
)


def _safe_int(raw: Any, default: int) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def classify_visual(v: dict[str, Any]) -> VisualClass:
    explicit = v.get("class")
    if explicit in _VALID_CLASSES:
        return explicit
    vtype = v.get("type") or v.get("visualType") or ""
    if vtype in SLICER_VISUAL_TYPES:
        return "slicer"
    if vtype in CARD_VISUAL_TYPES:
        return "kpi"
    if vtype in MATRIX_VISUAL_TYPES:
        return "matrix"
    if vtype in TABLE_VISUAL_TYPES:
        return "table"
    if vtype in DECORATIVE_VISUAL_TYPES:
        return "supporting"
    return "chart"  # safe fallback for any other native analytical visualType


def _estimate_slicer_size(v: dict[str, Any], p: dict) -> tuple[float, float]:
    # Formula source: skills/powerbi-report-authoring/references/slicers.md
    pad = p.get("visual_padding_px", 10)
    mode = v.get("slicer_mode") or "Dropdown"
    label_len = _safe_int(v.get("label_max_len") or len(v.get("title") or "") or 8, 8)
    width = (
        216
        if mode == "Between"
        else (120 if label_len <= 6 else p.get("slicer_w", 160))
    )

    if (v.get("type") or v.get("visualType")) == "advancedSlicerVisual":
        tiles = max(1, math.ceil(_safe_int(v.get("slicer_item_count") or 6, 6) / 5))
        height = tiles * 56.0
    elif (
        mode in ("Basic", "Single")
        or (v.get("type") or v.get("visualType")) == "listSlicer"
    ):
        header_h = 32 if v.get("slicer_header_shown", True) else 0
        n = _safe_int(v.get("slicer_item_count") or 8, 8)
        search_shown = v.get("slicer_search_shown")
        search_h = 32 if (search_shown if search_shown is not None else n > 10) else 0
        height = 2 * pad + header_h + search_h + n * 24 + 8
    else:  # Dropdown / Between / Before / After / Relative*
        chrome = 84 if (mode == "Between" and v.get("slicer_stacked")) else 60
        height = chrome + 2 * pad
    return width, max(height, p["slicer_min_px"])


def _estimate_card_size(v: dict[str, Any], p: dict) -> tuple[float, float]:
    fields = max(1, _safe_int(v.get("field_count") or 1, 1))
    width = fields * p.get("kpi_callout_w_px", 260)
    height = (
        104.0 if fields == 1 else 116.0
    )  # card.md: "100-120px accommodates value+label"
    if v.get("title"):
        height += 24  # separate title band above the value/label
    if _safe_int(v.get("label_max_len") or 0, 0) > 24:
        height += 16  # long label risks a second wrapped line
    return width, max(height, p["kpi_min_px"])


def _estimate_chart_size(v: dict[str, Any], p: dict) -> tuple[float, float]:
    vtype = v.get("type") or v.get("visualType") or ""
    height = float(p["chart_min_px"])
    if vtype in BAR_CHART_TYPES and not v.get("has_top_n_filter", False):
        # matches dashboard_audit.py's scrollbar_risk threshold
        height = max(height, 320.0)
    field_count = _safe_int(v.get("field_count") or 2, 2)
    label_len = _safe_int(v.get("label_max_len") or 8, 8)
    width = max(p.get("chart_min_w_px", 280), 40 * field_count + label_len * 4)
    if vtype in CARTESIAN_CHART_TYPES:
        # matches dashboard_audit.py's chart_taller_than_wide rule
        width = max(width, height * 1.3)
    return width, height


def _estimate_tabular_size(
    v: dict[str, Any], p: dict, min_h_key: str, min_w_key: str, col_w_key: str
) -> tuple[float, float]:
    cols = max(1, _safe_int(v.get("column_count") or 3, 3))
    width = cols * p.get(col_w_key, 120)
    return max(width, p.get(min_w_key, 280)), float(p.get(min_h_key, p["chart_min_px"]))


def _estimate_table_size(v: dict[str, Any], p: dict) -> tuple[float, float]:
    return _estimate_tabular_size(
        v, p, "table_min_px", "table_min_w_px", "table_col_w_px"
    )


def _estimate_matrix_size(v: dict[str, Any], p: dict) -> tuple[float, float]:
    return _estimate_tabular_size(
        v, p, "matrix_min_px", "matrix_min_w_px", "matrix_col_w_px"
    )


def _estimate_header_size(v: dict[str, Any], p: dict) -> tuple[float, float]:
    return 0.0, float(p["header_h"])  # width sentinel: caller gives it the full row


def _estimate_supporting_size(v: dict[str, Any], p: dict) -> tuple[float, float]:
    if (v.get("type") or v.get("visualType") or "") == "actionButton":
        return float(p.get("button_w", 120)), float(p.get("button_h", 32))
    nav = p.get("nav_px", 40)
    return float(nav * 3), float(nav)


_ESTIMATORS = {
    "header": _estimate_header_size,
    "slicer": _estimate_slicer_size,
    "kpi": _estimate_card_size,
    "chart": _estimate_chart_size,
    "table": _estimate_table_size,
    "matrix": _estimate_matrix_size,
    "supporting": _estimate_supporting_size,
}


def estimate_min_size(
    v: dict[str, Any], vclass: VisualClass | None = None, policy: dict | None = None
) -> tuple[int, int]:
    p = policy or REPORT_LAYOUT
    vclass = vclass or classify_visual(v)
    fn = _ESTIMATORS.get(vclass, _estimate_chart_size)
    w, h = fn(v, p)
    return max(1, math.ceil(w)), max(1, math.ceil(h))


def _extract_title(v: dict[str, Any]) -> str:
    entries = v.get("visual", {}).get("visualContainerObjects", {}).get("title") or []
    for e in entries:
        raw = (
            str(
                (e.get("properties") or {})
                .get("text", {})
                .get("expr", {})
                .get("Literal", {})
                .get("Value", "")
            )
            .strip("'")
            .strip()
        )
        if raw:
            return raw
    return ""


def sizing_spec_from_pbir_visual(v: dict[str, Any]) -> dict[str, Any]:
    vis = v.get("visual", {})
    vtype = vis.get("visualType", "")
    qs = vis.get("query", {}).get("queryState", {})
    field_count = sum(
        len(role.get("projections", []))
        for role in qs.values()
        if isinstance(role, dict)
    )
    mode = None
    try:
        mode = (
            vis.get("objects", {})["data"][0]["properties"]["mode"]["expr"]["Literal"][
                "Value"
            ]
        ).strip("'")
    except (KeyError, IndexError, TypeError, AttributeError):
        pass
    return {
        "type": vtype,
        "field_count": field_count,
        "column_count": field_count,
        "slicer_mode": mode,
        "label_max_len": len(_extract_title(v)),
    }
