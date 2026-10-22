"""Placing a visual on top of another must be reported when it happens.

`pbi_layout_compute` and `pbi_dashboard_layout` return non-overlapping rects and
the tool specs say to use them. Nothing enforces it, and when the agent picks
pixel positions itself `pbi_visual_add` still answers "status: created" — the
call did exactly what it was asked to.

That produced a real defect: five slicers laid out 352px wide on a 1440px page,
a width that fits four. The last two stacked almost exactly on top of each
other, and the only thing that ever noticed was the post-build audit, many turns
after the call that caused it.

A warning, not a refusal: layering is sometimes deliberate — a shape behind a
KPI row, a banner under a title — and a guard that blocks normal composition
gets designed around.
"""

from __future__ import annotations

import json

import pytest

from report.layout_guard import overlap_note, overlapping_visuals


@pytest.fixture
def page(tmp_path):
    """A report page; add visuals by rectangle."""
    root = tmp_path / "R.Report"
    visuals = root / "definition" / "pages" / "overview" / "visuals"
    visuals.mkdir(parents=True)

    def _add(name, x, y, w, h):
        d = visuals / name
        d.mkdir()
        (d / "visual.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "position": {"x": x, "y": y, "width": w, "height": h},
                    "visual": {"visualType": "slicer"},
                }
            ),
            encoding="utf-8",
        )

    _add.root = str(root)
    return _add


def _hits(page, name, x, y, w, h):
    return overlapping_visuals(page.root, "overview", name, x, y, w, h)


# ---------------------------------------------------------------------------
# the real incident
# ---------------------------------------------------------------------------


def test_the_slicer_collision_that_shipped_is_detected(page):
    """The exact rectangles from the report: 1080+352 vs 1088+344."""
    page("slicer_channel", 1080, 8, 352, 80)
    hits = _hits(page, "slicer_status", 1088, 8, 344, 80)
    assert [h[0] for h in hits] == ["slicer_channel"]


def test_the_corrected_spacing_is_clean(page):
    """272px x5 with 16px gutters on a 1440px page — the applied fix."""
    for i, n in enumerate(["s1", "s2", "s3", "s4"]):
        page(n, 8 + i * 288, 8, 272, 80)
    assert _hits(page, "s5", 8 + 4 * 288, 8, 272, 80) == []


def test_the_note_names_the_visual_underneath_and_the_way_out(page):
    page("slicer_channel", 1080, 8, 352, 80)
    note = overlap_note(page.root, "overview", "slicer_status", 1088, 8, 344, 80)
    assert "slicer_channel" in note
    assert "pbi_dashboard_layout" in note or "pbi_layout_compute" in note


# ---------------------------------------------------------------------------
# not crying wolf
# ---------------------------------------------------------------------------


def test_visuals_that_only_touch_are_not_overlapping(page):
    """Edge-to-edge is a normal grid, not a collision. Matches the audit's 1px rule."""
    page("a", 8, 8, 100, 80)
    assert _hits(page, "b", 108, 8, 100, 80) == []


def test_a_visual_does_not_collide_with_itself(page):
    """Safe to call after the write, when the new position is already on disk."""
    page("a", 8, 8, 100, 80)
    assert _hits(page, "a", 8, 8, 100, 80) == []


def test_the_header_banner_is_expected_to_sit_underneath(page):
    """Full-width chrome behind other visuals is the intended design."""
    page("header_banner", 0, 0, 1440, 80)
    assert _hits(page, "kpi", 8, 8, 200, 60) == []


def test_visuals_on_another_page_are_ignored(page, tmp_path):
    other = tmp_path / "R.Report" / "definition" / "pages" / "detail" / "visuals" / "z"
    other.mkdir(parents=True)
    (other / "visual.json").write_text(
        json.dumps({"name": "z", "position": {"x": 8, "y": 8, "width": 900, "height": 400}}),
        encoding="utf-8",
    )
    assert _hits(page, "new", 8, 8, 200, 80) == []


def test_a_clear_placement_produces_no_note(page):
    page("a", 8, 8, 100, 80)
    assert overlap_note(page.root, "overview", "b", 800, 8, 100, 80) == ""


def test_an_unreadable_neighbour_does_not_break_the_check(page):
    """A warning must never take down the write it is describing."""
    page("good", 1080, 8, 352, 80)
    bad = page.root + "/definition/pages/overview/visuals/bad"
    import os

    os.makedirs(bad)
    with open(bad + "/visual.json", "w", encoding="utf-8") as f:
        f.write("{ not json")
    assert [h[0] for h in _hits(page, "new", 1088, 8, 344, 80)] == ["good"]


def test_a_missing_report_path_is_not_an_error(page):
    assert overlapping_visuals("", "overview", "x", 0, 0, 10, 10) == []
