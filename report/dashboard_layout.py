
from __future__ import annotations

from typing import Any

from config import REPORT_LAYOUT
from report.layout_engine import compute_layout
from report.visual_sizing import classify_visual, estimate_min_size

_MAX_LAYOUT_ITERATIONS = 4
_SECTION_CLASS = {
    "slicers": "slicer",
    "kpis": "kpi",
    "charts": "chart",
    "supporting": "supporting",
}


def _wrap_by_capacity(
    sized_items: list[dict[str, Any]],
    available_w: float,
    gutter: float,
    max_per_row: int | None = None,
) -> list[list[dict[str, Any]]]:
    rows: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    cur_w = 0.0
    for it in sized_items:
        w = it["min_w"]
        added = w if not cur else w + gutter
        overflow = bool(cur) and (cur_w + added > available_w)
        at_cap = bool(max_per_row) and len(cur) >= max_per_row
        if cur and (it.get("row_break") or overflow or at_cap):
            rows.append(cur)
            cur, cur_w, added = [], 0.0, w
        cur.append(it)
        cur_w += added
    if cur:
        rows.append(cur)
    return rows


def _build_rows(
    sections: dict[str, Any],
    inner_w: float,
    gutter: float,
    policy: dict,
    diagnostics: list[str],
    max_charts_per_row: int | None = None,
) -> tuple[list[dict[str, Any]], tuple[str, float] | None]:
    rows: list[dict[str, Any]] = []
    overflow: tuple[str, float] | None = None
    # Larger gap for section boundaries
    section_gap = policy.get("section_gap_px", gutter)

    header = sections.get("header")
    if header:
        _, h = estimate_min_size(header, "header", policy)
        rows.append(
            {
                "height": h,
                "items": [{"name": header["name"], "weight": 1}],
                "gap_after": section_gap,
            }
        )

    def _section(key: str, fixed_height: bool, max_per_row: int | None = None) -> None:
        nonlocal overflow
        items = sections.get(key) or []
        if not items:
            return
        sized = []
        for it in items:
            if key == "tables":
                c = classify_visual(it)
                cls = c if c in ("table", "matrix") else "table"
            else:
                cls = _SECTION_CLASS[key]
            w, h = estimate_min_size(it, cls, policy)
            sized.append(
                {
                    "name": it["name"],
                    "min_w": w,
                    "min_h": h,
                    "row_break": bool(it.get("row_break")),
                }
            )

        wrapped = _wrap_by_capacity(sized, inner_w, gutter, max_per_row)
        if len(wrapped) > 1:
            total_w = sum(it["min_w"] for it in sized) + gutter * (len(sized) - 1)
            diagnostics.append(
                f"wrapped {len(sized)} {key} into {len(wrapped)} rows because they "
                f"need {int(total_w)}px total but only {int(inner_w)}px is available "
                "per row"
            )

        for group in wrapped:
            if len(group) == 1 and group[0]["min_w"] > inner_w:
                overflow = overflow or (group[0]["name"], group[0]["min_w"])
            row_floor_h = max(it["min_h"] for it in group)
            row_items = [{"name": it["name"], "weight": it["min_w"]} for it in group]
            rows.append(
                {
                    "height": row_floor_h if fixed_height else "fill",
                    "weight": row_floor_h,
                    "items": row_items,
                }
            )

    def _mark_section_boundary(_before: int) -> None:
        if len(rows) > _before:
            rows[-1]["gap_after"] = section_gap

    _before = len(rows)
    _section("slicers", fixed_height=True)
    _mark_section_boundary(_before)

    _before = len(rows)
    _section("kpis", fixed_height=True)
    _mark_section_boundary(_before)

    _before = len(rows)
    _section("charts", fixed_height=False, max_per_row=max_charts_per_row)
    _mark_section_boundary(_before)

    _before = len(rows)
    _section("tables", fixed_height=False)
    _mark_section_boundary(_before)

    _section("supporting", fixed_height=True)  # last section — no boundary after it

    return rows, overflow


def _required_height(rows: list[dict[str, Any]], margin: float, gutter: float) -> float:
    fixed = sum(r["height"] for r in rows if r["height"] != "fill")
    fill_floor = sum(r["weight"] for r in rows if r["height"] == "fill")
    gaps = sum(float(r.get("gap_after", gutter)) for r in rows[:-1])
    return 2 * margin + gaps + fixed + fill_floor


def compute_dashboard_layout(spec: dict[str, Any]) -> dict[str, Any]:
    policy = REPORT_LAYOUT
    sections = spec.get("sections") or {}
    if not any(
        sections.get(k)
        for k in ("header", "slicers", "kpis", "charts", "tables", "supporting")
    ):
        return {"error": "sections must contain at least one visual"}

    canvas_cfg = {
        "min_width": policy["canvas_min_w"],
        "preferred_width": policy["canvas_preferred_w"],
        "max_width": policy["canvas_max_w"],
        "min_height": policy["canvas_min_h"],
        "preferred_height": policy["canvas_preferred_h"],
        "max_height": policy["canvas_max_h"],
        **(spec.get("canvas") or {}),
    }
    margin = float(spec.get("margin_px", policy["margin_px"]))
    gutter = float(spec.get("gutter_px", policy["gutter_px"]))
    max_charts_per_row = spec.get("max_charts_per_row")

    raw_w, raw_h = spec.get("page_width"), spec.get("page_height")
    explicit_page = raw_w is not None and raw_h is not None
    if explicit_page:
        width, height = float(raw_w), float(raw_h)
    else:
        aspect = float(
            spec.get("aspect_ratio")
            or (canvas_cfg["preferred_width"] / canvas_cfg["preferred_height"])
        )
        if raw_w is not None:
            width = float(raw_w)
            height = width / aspect
        elif raw_h is not None:
            height = float(raw_h)
            width = height * aspect
        else:
            width, height = (
                canvas_cfg["preferred_width"],
                canvas_cfg["preferred_height"],
            )
        width = min(max(width, canvas_cfg["min_width"]), canvas_cfg["max_width"])
        height = min(max(height, canvas_cfg["min_height"]), canvas_cfg["max_height"])

    diagnostics: list[str] = []
    rows_spec: list[dict[str, Any]] = []
    wrap_notes: list[str] = []

    for _ in range(_MAX_LAYOUT_ITERATIONS):
        inner_w = width - 2 * margin
        wrap_notes = []
        rows_spec, overflow = _build_rows(
            sections, inner_w, gutter, policy, wrap_notes, max_charts_per_row
        )
        required_h = _required_height(rows_spec, margin, gutter)

        if explicit_page:
            # Growth is intentionally suppressed for an explicit page size —
            # respecting the caller's dimensions is legitimate — but silently
            # shrinking visuals below their minimum (compute_layout clamps
            # rather than errors) must not happen without a visible warning.
            if overflow:
                diagnostics.append(
                    f"explicit page_width={int(width)}px is narrower than "
                    f"{overflow[0]}'s {int(overflow[1])}px minimum — it will "
                    "render below its minimum readable width"
                )
            if required_h > height:
                diagnostics.append(
                    f"explicit page_height={int(height)}px is below the "
                    f"{int(required_h)}px required by row minimums — one or "
                    "more visuals will render below their minimum height"
                )

        grew = False
        if overflow and not explicit_page:
            needed_w = overflow[1] + 2 * margin
            new_w = min(max(needed_w, width), canvas_cfg["max_width"])
            if new_w > width:
                diagnostics.append(
                    f"widened canvas from {int(width)}px to {int(new_w)}px because "
                    f"{overflow[0]} needs {int(overflow[1])}px of width"
                )
                width, grew = new_w, True
        if required_h > height and not explicit_page:
            new_h = min(max(required_h, height), canvas_cfg["max_height"])
            if new_h > height:
                diagnostics.append(
                    f"grew canvas height from {int(height)}px to {int(new_h)}px to fit "
                    f"{len(rows_spec)} rows without shrinking any visual below its minimum"
                )
                height, grew = new_h, True
        if not grew:
            break
    else:
        diagnostics.append(
            f"layout did not fully stabilize after {_MAX_LAYOUT_ITERATIONS} "
            "iterations — using the last computed arrangement"
        )

    diagnostics.extend(wrap_notes)

    result = compute_layout(
        {
            "page_width": width,
            "page_height": height,
            "rows": rows_spec,
            "margin_px": margin,
            "gutter_px": gutter,
        }
    )
    if "error" in result:
        return result
    result["diagnostics"] = diagnostics
    return result


def plan_page_resize(
    page_w: float,
    page_h: float,
    target_w: float,
    target_h: float,
    visuals: list[dict[str, Any]],
) -> dict[str, Any]:
    adjustments: dict[str, dict[str, float]] = {}

    for v in visuals:
        name = v["name"]
        x, y = float(v["x"]), float(v["y"])
        w, h = float(v["width"]), float(v["height"])
        exempt = bool(v.get("exempt")) or name == "header_banner"
        min_w = 0.0 if exempt else float(v.get("min_w", 0))
        min_h = 0.0 if exempt else float(v.get("min_h", 0))
        new_w, new_h = w, h

        if name == "header_banner":
            new_w = target_w
        elif x + w > target_w:
            if x >= target_w:
                return {
                    "error": (
                        f"cannot resize to {int(target_w)}x{int(target_h)}: "
                        f"'{name}' is positioned at x={int(x)}, entirely "
                        f"outside the new {int(target_w)}px width — move it "
                        "first or choose a larger width"
                    )
                }
            candidate = target_w - x
            if candidate < min_w:
                return {
                    "error": (
                        f"cannot shrink to {int(target_w)}x{int(target_h)}: "
                        f"'{name}' needs >={int(min_w)}px wide at its "
                        f"current position, only {int(candidate)}px would "
                        "remain — move/resize it first or choose a larger "
                        "width"
                    )
                }
            new_w = candidate

        if y + h > target_h:
            if y >= target_h:
                return {
                    "error": (
                        f"cannot resize to {int(target_w)}x{int(target_h)}: "
                        f"'{name}' is positioned at y={int(y)}, entirely "
                        f"outside the new {int(target_h)}px height — move it "
                        "first or choose a larger height"
                    )
                }
            candidate = target_h - y
            if candidate < min_h:
                return {
                    "error": (
                        f"cannot shrink to {int(target_w)}x{int(target_h)}: "
                        f"'{name}' needs >={int(min_h)}px tall at its "
                        f"current position, only {int(candidate)}px would "
                        "remain — move/resize it first or choose a larger "
                        "height"
                    )
                }
            new_h = candidate

        if new_w != w or new_h != h:
            adjustments[name] = {"width": new_w, "height": new_h}

    return {"width": target_w, "height": target_h, "adjustments": adjustments}
