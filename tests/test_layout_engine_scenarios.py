"""Scenario matrix for the content-aware dashboard layout engine.

report/dashboard_layout.py (compute_dashboard_layout) is the orchestrator:
classify each requested visual -> estimate its minimum readable size
(report/visual_sizing.py) -> wrap slicers/charts/tables that don't fit the
available width -> grow the canvas (never shrink below a minimum) -> hand the
final row/weight spec to report/layout_engine.py's compute_layout solver.

Everything under test here is pure (no I/O, no randomness, no wall-clock), so
these tests call the real functions directly with no mocking.
"""

from __future__ import annotations

import json
from itertools import combinations

from config import REPORT_LAYOUT
from report.dashboard_layout import _wrap_by_capacity, compute_dashboard_layout
from report.layout_engine import compute_layout
from report.visual_sizing import classify_visual, estimate_min_size

# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def _chart(name, **kw):
    return {"name": name, "type": "clusteredColumnChart", **kw}


def _slicer(name, **kw):
    return {"name": name, "type": "slicer", **kw}


def _kpi(name, **kw):
    return {"name": name, "type": "cardVisual", **kw}


def _table(name, cols=3, **kw):
    return {"name": name, "type": "tableEx", "column_count": cols, **kw}


def _rects(result):
    assert "error" not in result, result.get("error")
    return result["rects"]


def _no_overlaps(rects):
    for (name_a, a), (name_b, b) in combinations(rects.items(), 2):
        ix = min(a["x"] + a["width"], b["x"] + b["width"]) - max(a["x"], b["x"])
        iy = min(a["y"] + a["height"], b["y"] + b["height"]) - max(a["y"], b["y"])
        assert not (ix > 0 and iy > 0), f"{name_a} overlaps {name_b}: {a} vs {b}"


def _all_in_bounds(result):
    page = result["page"]
    for name, r in result["rects"].items():
        assert r["x"] >= 0 and r["y"] >= 0, f"{name} has a negative origin: {r}"
        assert (
            r["x"] + r["width"] <= page["width"] + 1
        ), f"{name} exceeds page width: {r}"
        assert (
            r["y"] + r["height"] <= page["height"] + 1
        ), f"{name} exceeds page height: {r}"


# ---------------------------------------------------------------------------
# 1. one visual
# ---------------------------------------------------------------------------


def test_a_single_chart_fills_the_page():
    result = compute_dashboard_layout({"sections": {"charts": [_chart("c1")]}})
    rects = _rects(result)
    assert set(rects) == {"c1"}
    assert rects["c1"]["height"] >= REPORT_LAYOUT["chart_min_px"]
    _all_in_bounds(result)


# ---------------------------------------------------------------------------
# 2 & 6. many charts requiring additional rows / canvas growing vertically
# ---------------------------------------------------------------------------


def test_many_charts_wrap_into_extra_rows_and_grow_height():
    charts = [_chart(f"c{i}", field_count=6, label_max_len=20) for i in range(8)]
    result = compute_dashboard_layout({"sections": {"charts": charts}})
    rects = _rects(result)
    assert len(rects) == 8
    row_ys = {round(r["y"]) for r in rects.values()}
    assert (
        len(row_ys) > 1
    ), "8 wide charts on a default canvas must span more than one row"
    assert result["page"]["height"] > REPORT_LAYOUT["canvas_preferred_h"]
    assert any("grew canvas height" in d for d in result["diagnostics"])
    _no_overlaps(rects)
    _all_in_bounds(result)


def test_canvas_grows_vertically_for_extra_rows():
    charts = [_chart(f"c{i}") for i in range(6)]
    result = compute_dashboard_layout(
        {"sections": {"charts": charts}, "max_charts_per_row": 2}
    )
    assert len(_rects(result)) == 6
    assert result["page"]["height"] > REPORT_LAYOUT["canvas_preferred_h"]
    _no_overlaps(_rects(result))
    _all_in_bounds(result)


# ---------------------------------------------------------------------------
# 3 & 4. slicers fitting in one row / slicers that must wrap
# ---------------------------------------------------------------------------


def test_slicers_that_fit_stay_in_one_row():
    slicers = [_slicer(f"s{i}", label_max_len=5) for i in range(3)]
    result = compute_dashboard_layout(
        {"page_width": 1280, "page_height": 720, "sections": {"slicers": slicers}}
    )
    rects = _rects(result)
    row_ys = {round(r["y"]) for r in rects.values()}
    assert len(row_ys) == 1
    _no_overlaps(rects)


def test_slicers_that_overflow_wrap_to_a_second_row():
    slicers = [_slicer(f"s{i}", label_max_len=30) for i in range(8)]
    result = compute_dashboard_layout(
        {"page_width": 960, "page_height": 720, "sections": {"slicers": slicers}}
    )
    rects = _rects(result)
    row_ys = {round(r["y"]) for r in rects.values()}
    assert len(row_ys) > 1
    assert any("wrapped 8 slicers" in d for d in result["diagnostics"])
    _no_overlaps(rects)
    _all_in_bounds(result)


# ---------------------------------------------------------------------------
# 5. a table requiring more width
# ---------------------------------------------------------------------------


def test_a_wide_table_grows_canvas_width():
    result = compute_dashboard_layout({"sections": {"tables": [_table("t1", cols=12)]}})
    assert result["page"]["width"] >= 12 * REPORT_LAYOUT["table_col_w_px"]
    _all_in_bounds(result)


# ---------------------------------------------------------------------------
# 7. explicit page dimensions
# ---------------------------------------------------------------------------


def test_explicit_page_dims_are_respected_exactly():
    result = compute_dashboard_layout(
        {
            "page_width": 1600,
            "page_height": 900,
            "sections": {"charts": [_chart("c1")]},
        }
    )
    assert result["page"]["width"] == 1600
    assert result["page"]["height"] == 900
    assert result["diagnostics"] == []


# ---------------------------------------------------------------------------
# 8. minimum visual-size enforcement
# ---------------------------------------------------------------------------


def test_no_visual_is_ever_sized_below_its_class_floor():
    spec = {
        "sections": {
            "header": {"name": "hdr"},
            "slicers": [_slicer("s1"), _slicer("s2")],
            "kpis": [_kpi("k1", field_count=2), _kpi("k2")],
            "charts": [_chart("c1"), _chart("c2", field_count=8, label_max_len=30)],
            "tables": [_table("tb1", cols=5)],
            "supporting": [{"name": "sp1", "type": "textbox"}],
        }
    }
    requests = {
        v["name"]: v
        for k in ("slicers", "kpis", "charts", "tables", "supporting")
        for v in spec["sections"][k]
    }
    requests["hdr"] = spec["sections"]["header"]

    result = compute_dashboard_layout(spec)
    rects = _rects(result)
    for name, rect in rects.items():
        vclass = classify_visual(requests[name])
        min_w, min_h = estimate_min_size(requests[name], vclass)
        assert rect["height"] >= min_h - REPORT_LAYOUT["grid_px"], (name, rect, min_h)
        if vclass != "header":  # header's floor is height-only, width is "fill the row"
            assert rect["width"] >= min_w - REPORT_LAYOUT["grid_px"], (
                name,
                rect,
                min_w,
            )
    _no_overlaps(rects)
    _all_in_bounds(result)


# ---------------------------------------------------------------------------
# 9. grid snapping
# ---------------------------------------------------------------------------


def test_all_rects_are_grid_snapped():
    grid = REPORT_LAYOUT["grid_px"]
    spec = {
        "sections": {
            "slicers": [_slicer("s1"), _slicer("s2"), _slicer("s3")],
            "kpis": [_kpi("k1"), _kpi("k2"), _kpi("k3")],
            "charts": [_chart("c1"), _chart("c2")],
        }
    }
    result = compute_dashboard_layout(spec)
    for rect in _rects(result).values():
        for key in ("x", "y", "width", "height"):
            assert rect[key] % grid == 0, rect


# ---------------------------------------------------------------------------
# 10. overlap prevention (mixed sections)
# ---------------------------------------------------------------------------


def test_no_two_visuals_overlap_across_all_sections():
    spec = {
        "sections": {
            "header": {"name": "hdr"},
            "slicers": [_slicer("s1"), _slicer("s2"), _slicer("s3")],
            "kpis": [_kpi("k1"), _kpi("k2"), _kpi("k3"), _kpi("k4")],
            "charts": [_chart("c1"), _chart("c2"), _chart("c3")],
            "tables": [_table("t1", cols=6)],
        }
    }
    result = compute_dashboard_layout(spec)
    _no_overlaps(_rects(result))


# ---------------------------------------------------------------------------
# 11. all visuals staying within canvas bounds
# ---------------------------------------------------------------------------


def test_all_visuals_stay_within_canvas_bounds():
    spec = {
        "sections": {
            "header": {"name": "hdr"},
            "slicers": [_slicer(f"s{i}") for i in range(6)],
            "kpis": [_kpi(f"k{i}") for i in range(5)],
            "charts": [_chart(f"c{i}") for i in range(7)],
        }
    }
    result = compute_dashboard_layout(spec)
    _all_in_bounds(result)


# ---------------------------------------------------------------------------
# 12. low-density layouts remaining valid without overlap or clipping
# ---------------------------------------------------------------------------


def test_sparse_page_still_produces_valid_non_overlapping_layout():
    result = compute_dashboard_layout(
        {
            "page_width": 1920,
            "page_height": 1080,
            "sections": {"charts": [_chart("lonely_chart")]},
        }
    )
    rects = _rects(result)
    assert rects["lonely_chart"]["height"] >= REPORT_LAYOUT["chart_min_px"]
    _no_overlaps(rects)
    _all_in_bounds(result)


# ---------------------------------------------------------------------------
# determinism + diagnostics
# ---------------------------------------------------------------------------


def test_identical_spec_produces_identical_output_across_calls():
    spec = {
        "sections": {
            "slicers": [_slicer(f"s{i}") for i in range(5)],
            "charts": [_chart(f"c{i}") for i in range(4)],
        }
    }
    a = compute_dashboard_layout(spec)
    b = compute_dashboard_layout(spec)
    assert a == b


def test_growth_and_wrap_decisions_produce_plain_english_diagnostics():
    spec = {
        "page_width": 900,
        "page_height": 600,
        "sections": {
            "slicers": [_slicer(f"s{i}", label_max_len=30) for i in range(6)],
            "charts": [_chart(f"c{i}", field_count=6) for i in range(6)],
        },
    }
    result = compute_dashboard_layout(spec)
    assert result["diagnostics"], "expected at least one diagnostic"
    assert all(isinstance(d, str) and d for d in result["diagnostics"])


def test_missing_sections_returns_an_error_not_a_crash():
    result = compute_dashboard_layout({"sections": {}})
    assert "error" in result


# ---------------------------------------------------------------------------
# regression: explicit page dims too small for content still warn, they
# don't silently shrink a visual below its minimum with no diagnostic
# ---------------------------------------------------------------------------


def test_explicit_page_height_too_small_produces_a_diagnostic():
    spec = {
        "page_width": 1280,
        "page_height": 150,  # far below what header+3 KPIs need
        "sections": {
            "header": {"name": "hdr"},
            "kpis": [_kpi("k1"), _kpi("k2"), _kpi("k3")],
        },
    }
    result = compute_dashboard_layout(spec)
    assert "error" not in result
    assert any("required by row minimums" in d for d in result["diagnostics"])
    assert result["page"]["height"] == 150  # respected exactly, per the contract


def test_explicit_page_width_too_narrow_for_a_lone_item_produces_a_diagnostic():
    spec = {
        "page_width": 200,
        "page_height": 720,
        "sections": {"tables": [_table("t1", cols=12)]},
    }
    result = compute_dashboard_layout(spec)
    assert "error" not in result
    assert any("minimum width" in d or "minimum" in d for d in result["diagnostics"])
    assert result["page"]["width"] == 200


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


def test_classify_visual_maps_known_types_to_the_right_class():
    assert classify_visual({"type": "slicer"}) == "slicer"
    assert classify_visual({"type": "listSlicer"}) == "slicer"
    assert classify_visual({"type": "cardVisual"}) == "kpi"
    assert classify_visual({"type": "kpi"}) == "kpi"
    assert classify_visual({"type": "pivotTable"}) == "matrix"
    assert classify_visual({"type": "tableEx"}) == "table"
    assert classify_visual({"type": "textbox"}) == "supporting"
    assert classify_visual({"type": "clusteredColumnChart"}) == "chart"


def test_classify_visual_falls_back_to_chart_for_unknown_types():
    assert classify_visual({"type": "someBrandNewVisualType"}) == "chart"
    assert classify_visual({"type": "pieChart"}) == "chart"


def test_classify_visual_explicit_class_override_wins():
    assert classify_visual({"type": "slicer", "class": "supporting"}) == "supporting"


# ---------------------------------------------------------------------------
# sizing formulas
# ---------------------------------------------------------------------------


def test_slicer_basic_mode_height_matches_the_slicers_md_worked_example():
    # skills/powerbi-report-authoring/references/slicers.md: 5-item Basic slicer,
    # VCO padding 10/10, header + search shown -> 20+32+32+(5*24)+8 = 212px.
    v = {
        "type": "listSlicer",
        "slicer_mode": "Basic",
        "slicer_item_count": 5,
        "slicer_header_shown": True,
        "slicer_search_shown": True,
    }
    _, height = estimate_min_size(v, "slicer")
    assert height == 212


def test_slicer_basic_mode_height_scales_with_item_count():
    v_small = {"type": "listSlicer", "slicer_mode": "Basic", "slicer_item_count": 3}
    v_large = {"type": "listSlicer", "slicer_mode": "Basic", "slicer_item_count": 15}
    _, h_small = estimate_min_size(v_small, "slicer")
    _, h_large = estimate_min_size(v_large, "slicer")
    assert h_large > h_small


def test_slicer_dropdown_height_respects_the_min_px_floor():
    _, height = estimate_min_size({"type": "slicer"}, "slicer")
    assert height >= REPORT_LAYOUT["slicer_min_px"]


def test_card_width_scales_with_field_count():
    w1, _ = estimate_min_size({"field_count": 1}, "kpi")
    w3, _ = estimate_min_size({"field_count": 3}, "kpi")
    assert w3 > w1
    assert w3 >= 3 * REPORT_LAYOUT["kpi_callout_w_px"]


def test_chart_bar_without_top_n_gets_the_scrollbar_risk_height_bump():
    v = {"type": "clusteredBarChart", "has_top_n_filter": False}
    _, height = estimate_min_size(v, "chart")
    assert height >= 320


def test_chart_bar_with_top_n_does_not_get_the_height_bump():
    v = {"type": "clusteredBarChart", "has_top_n_filter": True}
    _, height = estimate_min_size(v, "chart")
    assert height == REPORT_LAYOUT["chart_min_px"]


def test_table_width_scales_with_column_count():
    w_narrow, _ = estimate_min_size({"column_count": 3}, "table")
    w_wide, _ = estimate_min_size({"column_count": 12}, "table")
    assert w_wide > w_narrow
    assert w_wide >= 12 * REPORT_LAYOUT["table_col_w_px"]


# ---------------------------------------------------------------------------
# _wrap_by_capacity
# ---------------------------------------------------------------------------


def _sized(name, w, h=200, row_break=False):
    return {"name": name, "min_w": w, "min_h": h, "row_break": row_break}


def test_wrap_by_capacity_preserves_input_order_and_never_reorders():
    items = [_sized("a", 300), _sized("b", 300), _sized("c", 300), _sized("d", 300)]
    rows = _wrap_by_capacity(items, available_w=650, gutter=10)
    flat = [it["name"] for row in rows for it in row]
    assert flat == ["a", "b", "c", "d"]
    assert len(rows) > 1


def test_wrap_by_capacity_honors_row_break():
    items = [_sized("a", 100), _sized("b", 100, row_break=True), _sized("c", 100)]
    rows = _wrap_by_capacity(items, available_w=1000, gutter=10)
    assert [it["name"] for it in rows[0]] == ["a"]
    assert [it["name"] for it in rows[1]] == ["b", "c"]


def test_wrap_by_capacity_honors_max_per_row():
    items = [_sized(str(i), 50) for i in range(5)]
    rows = _wrap_by_capacity(items, available_w=10_000, gutter=10, max_per_row=2)
    assert [len(r) for r in rows] == [2, 2, 1]


# ---------------------------------------------------------------------------
# compute_layout contract pin (pbi_layout_compute must not have changed)
# ---------------------------------------------------------------------------


def test_compute_layout_contract_is_unchanged():
    spec = {
        "page_width": 1280,
        "page_height": 720,
        "rows": [
            {"height": 80, "items": [{"name": "header"}]},
            {"height": "fill", "items": [{"name": "a"}, {"name": "b", "weight": 2}]},
        ],
    }
    result = compute_layout(spec)
    # These are REPORT_LAYOUT's margin_px/gutter_px, not free-floating numbers —
    # a pin that disagrees with config.py is pinning nothing.
    assert result["page"] == {
        "width": 1280,
        "height": 720,
        "grid_px": 8,
        "margin_px": 24,
        "gutter_px": 16,
    }
    assert result["rects"]["header"] == {"x": 24, "y": 24, "width": 1232, "height": 80}
    # row 2 has 2 items (one gutter): inner_w = 1280 - 2*24 - 16 = 1216, split
    # 1:2 -> a=~405.33 snapped to 408, b=~810.67 snapped to 808. 408+16+808
    # = 1232, i.e. the row still spans the full inner width after snapping.
    a, b = result["rects"]["a"], result["rects"]["b"]
    assert a["width"] == 408
    assert b["width"] == 808
    assert a["x"] == 24
    assert b["x"] == 24 + 408 + 16
    assert a["y"] == b["y"] == 120  # 24 + 80 + 16 gutter


# ---------------------------------------------------------------------------
# regression: invalid canvas / row dimensions are rejected, not silently
# turned into negative rects
# ---------------------------------------------------------------------------


def test_negative_or_zero_page_dimensions_are_rejected():
    base = {"rows": [{"height": "fill", "items": [{"name": "v1"}]}]}
    for bad in (-100, 0):
        result = compute_layout({**base, "page_width": bad, "page_height": 720})
        assert "error" in result, (bad, result)
        result = compute_layout({**base, "page_width": 1280, "page_height": bad})
        assert "error" in result, (bad, result)


def test_negative_or_zero_fixed_row_height_is_rejected():
    for bad in (-50, 0):
        result = compute_layout(
            {
                "page_width": 1280,
                "page_height": 720,
                "rows": [{"height": bad, "items": [{"name": "v1"}]}],
            }
        )
        assert "error" in result, (bad, result)


def test_negative_percentage_row_height_is_rejected():
    result = compute_layout(
        {
            "page_width": 1280,
            "page_height": 720,
            "rows": [{"height": "-10%", "items": [{"name": "v1"}]}],
        }
    )
    assert "error" in result


def test_no_visual_ever_gets_a_negative_dimension_from_the_orchestrator():
    # A regression pin one level up: compute_dashboard_layout must never be
    # able to feed compute_layout a spec that produces negative rects.
    result = compute_dashboard_layout(
        {"page_width": 1280, "page_height": 720, "sections": {"charts": [_chart("c1")]}}
    )
    for rect in result["rects"].values():
        assert rect["width"] > 0 and rect["height"] > 0


# ---------------------------------------------------------------------------
# regression: malformed numeric sizing hints degrade to a default instead
# of raising
# ---------------------------------------------------------------------------


def test_malformed_label_max_len_does_not_crash():
    w, h = estimate_min_size({"label_max_len": "abc"}, "slicer")
    assert w > 0 and h > 0


def test_malformed_numeric_hints_do_not_crash_any_class():
    garbage = "not-a-number"
    cases = [
        ({"field_count": garbage}, "kpi"),
        ({"field_count": garbage, "label_max_len": garbage}, "chart"),
        ({"slicer_item_count": garbage, "slicer_mode": "Basic"}, "slicer"),
        ({"slicer_item_count": garbage, "type": "advancedSlicerVisual"}, "slicer"),
        ({"column_count": garbage}, "table"),
        ({"column_count": garbage}, "matrix"),
        ({"label_max_len": garbage}, "slicer"),
    ]
    for v, vclass in cases:
        w, h = estimate_min_size(v, vclass)
        assert w > 0 and h > 0, (v, vclass)


def test_malformed_hint_falls_back_to_the_same_default_as_a_missing_hint():
    # A garbage string should behave like the hint was never supplied, not
    # like some arbitrary other number.
    with_missing = estimate_min_size({}, "kpi")
    with_garbage = estimate_min_size({"field_count": "nope"}, "kpi")
    assert with_missing == with_garbage


def test_dashboard_layout_survives_malformed_hints_end_to_end():
    spec = {
        "sections": {
            "slicers": [{"name": "s1", "type": "slicer", "label_max_len": "bad"}],
            "kpis": [{"name": "k1", "type": "cardVisual", "field_count": "bad"}],
            "charts": [
                {"name": "c1", "type": "clusteredColumnChart", "field_count": "bad"}
            ],
            "tables": [{"name": "t1", "type": "tableEx", "column_count": "bad"}],
        }
    }
    result = compute_dashboard_layout(spec)
    assert "error" not in result
    _no_overlaps(result["rects"])


# ---------------------------------------------------------------------------
# regression: every slicer visualType is audited, not just literal "slicer"
# ---------------------------------------------------------------------------


def _write_report_fixture(tmp_path, page_w, page_h, visuals):
    """Minimal on-disk PBIR fixture: definition/pages/p1/{page.json,visuals/*}."""
    import json as _json

    page_dir = tmp_path / "definition" / "pages" / "p1"
    (page_dir / "visuals").mkdir(parents=True)
    (page_dir / "page.json").write_text(
        _json.dumps({"displayName": "Overview", "width": page_w, "height": page_h}),
        encoding="utf-8",
    )
    for name, visual_type, x, y, w, h in visuals:
        vdir = page_dir / "visuals" / name
        vdir.mkdir()
        (vdir / "visual.json").write_text(
            _json.dumps(
                {
                    "position": {"x": x, "y": y, "width": w, "height": h},
                    "visual": {
                        "visualType": visual_type,
                        "query": {
                            "queryState": {
                                "Values": {
                                    "projections": [
                                        {
                                            "field": {
                                                "Column": {
                                                    "Expression": {
                                                        "SourceRef": {"Entity": "T"}
                                                    },
                                                    "Property": "F",
                                                }
                                            }
                                        }
                                    ]
                                }
                            }
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
    return str(tmp_path)


def test_list_slicer_gets_the_same_min_height_audit_as_slicer(tmp_path, monkeypatch):
    monkeypatch.setattr("report.dashboard_audit._registered_theme", lambda p: None)
    monkeypatch.setattr("report.dashboard_audit._allowed_field_keys", lambda p: None)
    report_path = _write_report_fixture(
        tmp_path,
        1280,
        720,
        [("thin_list_slicer", "listSlicer", 0, 0, 200, 20)],  # far below slicer_min_px
    )
    from report.dashboard_audit import _audit_dashboard

    result = _audit_dashboard(report_path)
    checks = {e["check"] for page in result["pages"].values() for e in page["errors"]}
    assert "slicer_too_short" in checks, result


# ---------------------------------------------------------------------------
# pbi_report_resize_page — plan_page_resize (pure) and the tool handler
# ---------------------------------------------------------------------------


def _chart_min_size():
    """The real minimum size estimate_min_size() gives a clusteredColumnChart
    shaped exactly like _write_report_fixture's — derived from the actual
    function, never hardcoded, so these tests can't silently drift from
    visual_sizing.py's formula."""
    from report.visual_sizing import (
        classify_visual,
        estimate_min_size,
        sizing_spec_from_pbir_visual,
    )

    v = {
        "visual": {
            "visualType": "clusteredColumnChart",
            "query": {
                "queryState": {
                    "Values": {
                        "projections": [
                            {
                                "field": {
                                    "Column": {
                                        "Expression": {"SourceRef": {"Entity": "T"}},
                                        "Property": "F",
                                    }
                                }
                            }
                        ]
                    }
                }
            },
        }
    }
    vclass = classify_visual({"type": "clusteredColumnChart"})
    return estimate_min_size(sizing_spec_from_pbir_visual(v), vclass)


def test_plan_page_resize_growth_syncs_banner_and_leaves_other_visuals_untouched():
    from report.dashboard_layout import plan_page_resize

    visuals = [
        {
            "name": "header_banner",
            "x": 0,
            "y": 0,
            "width": 1280,
            "height": 64,
            "min_w": 0,
            "min_h": 0,
            "exempt": True,
        },
        {
            "name": "chart_1",
            "x": 24,
            "y": 100,
            "width": 400,
            "height": 300,
            "min_w": 280,
            "min_h": 200,
            "exempt": False,
        },
    ]
    result = plan_page_resize(1280, 720, 1600, 900, visuals)
    assert "error" not in result, result
    assert result["width"] == 1600
    assert result["height"] == 900
    assert result["adjustments"] == {"header_banner": {"width": 1600, "height": 64}}


def test_plan_page_resize_shrink_clamps_a_visual_down_to_its_minimum():
    from report.dashboard_layout import plan_page_resize

    # x=40, target width 340 -> candidate = 340-40 = 300, exactly its min_w
    visuals = [
        {
            "name": "chart_1",
            "x": 40,
            "y": 100,
            "width": 500,
            "height": 300,
            "min_w": 300,
            "min_h": 200,
            "exempt": False,
        },
    ]
    result = plan_page_resize(1280, 720, 340, 720, visuals)
    assert "error" not in result, result
    assert result["adjustments"]["chart_1"]["width"] == 300
    assert result["adjustments"]["chart_1"]["height"] == 300  # y+h=400<=720, unaffected


def test_plan_page_resize_refuses_when_a_visual_cannot_fit_its_minimum():
    from report.dashboard_layout import plan_page_resize

    # x=40, target width 300 -> candidate = 260 < min_w 300
    visuals = [
        {
            "name": "chart_1",
            "x": 40,
            "y": 100,
            "width": 500,
            "height": 300,
            "min_w": 300,
            "min_h": 200,
            "exempt": False,
        },
    ]
    result = plan_page_resize(1280, 720, 300, 720, visuals)
    assert "error" in result
    assert "chart_1" in result["error"]
    assert "adjustments" not in result


def test_plan_page_resize_refuses_when_a_visual_sits_entirely_outside_the_new_canvas():
    from report.dashboard_layout import plan_page_resize

    visuals = [
        {
            "name": "chart_1",
            "x": 1000,
            "y": 100,
            "width": 200,
            "height": 300,
            "min_w": 280,
            "min_h": 200,
            "exempt": False,
        },
    ]
    result = plan_page_resize(1280, 720, 900, 720, visuals)
    assert "error" in result
    assert "chart_1" in result["error"]
    assert "outside" in result["error"]


def test_resize_page_tool_grows_canvas_and_syncs_banner_on_disk(tmp_path):
    from llm.pbi_tool_exec import execute_pbi_tool

    report_path = _write_report_fixture(
        tmp_path,
        1280,
        720,
        [
            ("header_banner", "textbox", 0, 0, 1280, 64),
            ("chart_1", "clusteredColumnChart", 24, 100, 400, 300),
        ],
    )
    result = execute_pbi_tool(
        "pbi_report_resize_page",
        {"report_path": report_path, "page": "p1", "width": 1600, "height": 900},
    )
    assert result.startswith("Resized page 'p1'"), result

    page_dir = tmp_path / "definition" / "pages" / "p1"
    page_json = json.loads((page_dir / "page.json").read_text(encoding="utf-8"))
    assert page_json["width"] == 1600
    assert page_json["height"] == 900

    banner_json = json.loads(
        (page_dir / "visuals" / "header_banner" / "visual.json").read_text(
            encoding="utf-8"
        )
    )
    assert banner_json["position"]["width"] == 1600

    chart_json = json.loads(
        (page_dir / "visuals" / "chart_1" / "visual.json").read_text(encoding="utf-8")
    )
    assert chart_json["position"]["width"] == 400  # untouched, still fits


def test_resize_page_tool_shrinks_and_clamps_a_visual_on_disk(tmp_path):
    from llm.pbi_tool_exec import execute_pbi_tool

    min_w, _min_h = _chart_min_size()
    x = 900
    target_w = x + min_w  # clamped width lands exactly at min_w
    assert 960 <= target_w <= 1920, target_w  # sanity-check the test's own setup
    page_dir = tmp_path / "definition" / "pages" / "p1"
    _write_report_fixture(
        tmp_path, 1280, 720, [("chart_1", "clusteredColumnChart", x, 100, 500, 300)]
    )

    result = execute_pbi_tool(
        "pbi_report_resize_page",
        {
            "report_path": str(tmp_path),
            "page": "p1",
            "width": target_w,
            "height": 720,
        },
    )
    assert result.startswith("Resized page 'p1'"), result
    assert "chart_1" in result

    chart_json = json.loads(
        (page_dir / "visuals" / "chart_1" / "visual.json").read_text(encoding="utf-8")
    )
    assert chart_json["position"]["width"] == min_w


def test_resize_page_tool_refuses_and_writes_nothing_when_a_visual_cannot_shrink_enough(
    tmp_path,
):
    from llm.pbi_tool_exec import execute_pbi_tool

    min_w, _min_h = _chart_min_size()
    x = 900
    target_w = x + min_w - 1  # one px short of the visual's own floor
    assert 960 <= target_w <= 1920, target_w
    report_path = _write_report_fixture(
        tmp_path, 1280, 720, [("chart_1", "clusteredColumnChart", x, 100, 500, 300)]
    )
    page_dir = tmp_path / "definition" / "pages" / "p1"
    page_json_path = page_dir / "page.json"
    chart_json_path = page_dir / "visuals" / "chart_1" / "visual.json"
    before_page = page_json_path.read_text(encoding="utf-8")
    before_chart = chart_json_path.read_text(encoding="utf-8")

    result = execute_pbi_tool(
        "pbi_report_resize_page",
        {"report_path": report_path, "page": "p1", "width": target_w, "height": 720},
    )
    assert result.startswith("ERROR"), result
    assert "chart_1" in result
    assert page_json_path.read_text(encoding="utf-8") == before_page
    assert chart_json_path.read_text(encoding="utf-8") == before_chart


def test_resize_page_tool_refuses_and_writes_nothing_when_a_visual_is_entirely_outside_canvas(
    tmp_path,
):
    from llm.pbi_tool_exec import execute_pbi_tool

    report_path = _write_report_fixture(
        tmp_path, 1280, 720, [("chart_1", "clusteredColumnChart", 1000, 100, 200, 300)]
    )
    page_dir = tmp_path / "definition" / "pages" / "p1"
    page_json_path = page_dir / "page.json"
    chart_json_path = page_dir / "visuals" / "chart_1" / "visual.json"
    before_page = page_json_path.read_text(encoding="utf-8")
    before_chart = chart_json_path.read_text(encoding="utf-8")

    result = execute_pbi_tool(
        "pbi_report_resize_page",
        {"report_path": report_path, "page": "p1", "width": 960, "height": 720},
    )
    assert result.startswith("ERROR"), result
    assert "chart_1" in result
    assert "outside" in result
    assert page_json_path.read_text(encoding="utf-8") == before_page
    assert chart_json_path.read_text(encoding="utf-8") == before_chart


def test_resize_page_tool_refuses_and_writes_nothing_when_target_is_outside_the_allowed_range(
    tmp_path,
):
    from llm.pbi_tool_exec import execute_pbi_tool

    report_path = _write_report_fixture(
        tmp_path, 1280, 720, [("chart_1", "clusteredColumnChart", 24, 100, 400, 300)]
    )
    page_json_path = tmp_path / "definition" / "pages" / "p1" / "page.json"
    before_page = page_json_path.read_text(encoding="utf-8")

    result = execute_pbi_tool(
        "pbi_report_resize_page",
        {"report_path": report_path, "page": "p1", "width": 5000, "height": 720},
    )
    assert result.startswith("ERROR"), result
    assert "width" in result
    assert page_json_path.read_text(encoding="utf-8") == before_page
