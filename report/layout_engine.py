from __future__ import annotations

import math
from typing import Any

from config import REPORT_DENSITY, REPORT_LAYOUT


def _snap(value: float, grid: int) -> int:
    return math.floor(value / grid + 0.5) * grid


def canvas_coverage(
    boxes: list[tuple[float, float]], page_w: float, page_h: float
) -> float:
    if not page_w or not page_h:
        return 1.0
    covered = sum(w * h for w, h in boxes)
    return covered / (page_w * page_h)


def is_under_filled(
    boxes: list[tuple[float, float]],
    page_w: float,
    page_h: float,
    data_visual_count: int,
    density: dict | None = None,
) -> bool:
    d = density or REPORT_DENSITY
    if data_visual_count < d["coverage_min_visuals"]:
        return False
    return canvas_coverage(boxes, page_w, page_h) < d["min_coverage_frac"]


def compute_layout(spec: dict[str, Any]) -> dict[str, Any]:
    try:
        page_w = float(spec["page_width"])
        page_h = float(spec["page_height"])
    except (KeyError, TypeError, ValueError):
        return {
            "error": "page_width and page_height (numbers) are required — read them via pbi_report_info"
        }
    if page_w <= 0 or page_h <= 0:
        return {"error": "page_width and page_height must be positive"}
    rows = spec.get("rows")
    if not isinstance(rows, list) or not rows:
        return {"error": "rows must be a non-empty array"}

    grid = REPORT_LAYOUT["grid_px"]
    margin = float(spec.get("margin_px", REPORT_LAYOUT["margin_px"]))
    gutter = float(spec.get("gutter_px", REPORT_LAYOUT["gutter_px"]))

    names: set[str] = set()
    fixed_total = 0.0
    fill_weight_total = 0.0
    heights: list[float | None] = []
    gap_after: list[float] = []
    for i, row in enumerate(rows):
        items = row.get("items")
        if not isinstance(items, list) or not items:
            return {"error": f"rows[{i}].items must be a non-empty array"}
        for it in items:
            n = (it or {}).get("name", "")
            if not n or not isinstance(n, str):
                return {"error": f"rows[{i}] has an item without a name"}
            if n in names:
                return {
                    "error": f"duplicate visual name {n!r} — names must be unique per page"
                }
            names.add(n)
        h = row.get("height", "fill")
        if isinstance(h, (int, float)):
            if h <= 0:
                return {"error": f"rows[{i}].height must be positive (got {h!r})"}
            heights.append(float(h))
            fixed_total += float(h)
        elif isinstance(h, str) and h.endswith("%"):
            try:
                px = page_h * float(h[:-1]) / 100.0
            except ValueError:
                return {"error": f"rows[{i}].height {h!r} is not a valid percentage"}
            if px <= 0:
                return {
                    "error": f"rows[{i}].height {h!r} resolves to a non-positive height"
                }
            heights.append(px)
            fixed_total += px
        elif h == "fill":
            heights.append(None)
            fill_weight_total += float(row.get("weight", 1) or 1)
        else:
            return {
                "error": f"rows[{i}].height must be a px number, 'NN%', or 'fill' (got {h!r})"
            }
        if i < len(rows) - 1:
            gap_after.append(float(row.get("gap_after", gutter)))

    inner_h = page_h - 2 * margin - sum(gap_after)
    remaining = inner_h - fixed_total
    if remaining < 0 and fill_weight_total:
        return {
            "error": f"fixed row heights ({fixed_total:.0f}px) exceed the usable page height ({inner_h:.0f}px)"
        }

    rects: dict[str, dict[str, int]] = {}
    y = _snap(margin, grid)
    for idx, (row, h) in enumerate(zip(rows, heights)):
        if h is None:
            h = remaining * float(row.get("weight", 1) or 1) / fill_weight_total
        items = row["items"]
        inner_w = page_w - 2 * margin - gutter * (len(items) - 1)
        item_weight_total = sum(float((it or {}).get("weight", 1) or 1) for it in items)
        x = _snap(margin, grid)
        for it in items:
            w = inner_w * float(it.get("weight", 1) or 1) / item_weight_total
            rect = {
                "x": x,
                "y": y,
                "width": _snap(w, grid),
                "height": _snap(h, grid),
            }
            rect["width"] = min(rect["width"], _snap(page_w, grid) - rect["x"])
            rect["height"] = min(rect["height"], _snap(page_h, grid) - rect["y"])
            rects[it["name"]] = rect
            x = _snap(x + w + gutter, grid)
        y += h
        if idx < len(gap_after):
            y += gap_after[idx]
        y = _snap(y, grid)

    return {
        "page": {
            "width": int(page_w),
            "height": int(page_h),
            "grid_px": grid,
            "margin_px": int(margin),
            "gutter_px": int(gutter),
        },
        "rects": rects,
    }
