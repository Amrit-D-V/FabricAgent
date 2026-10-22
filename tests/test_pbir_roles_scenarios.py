"""The role allow-list, and reading a visual back by role.

Power BI drops roles it does not recognise and renders the visual empty rather
than erroring, so "the field is bound" and "the field is visible" are different
questions. Everything here is about being able to answer the second one.

The allow-list is deliberately partial: an unrecognised visual type must
produce NO findings. Reports fetched from a workspace routinely contain types
this repo has never authored, and flagging those would bury real defects.
"""

from __future__ import annotations

import json

import pytest

from report.pbir_roles import (
    describe_query_state,
    known_roles,
    unsupported_roles,
)


def _qs(**roles) -> dict:
    """queryState with one measure projection per named role."""
    return {
        r: {
            "projections": [
                {
                    "field": {
                        "Measure": {
                            "Expression": {"SourceRef": {"Entity": "M"}},
                            "Property": f,
                        }
                    },
                    "queryRef": f"M.{f}",
                }
            ]
        }
        for r, f in roles.items()
    }


# ---------------------------------------------------------------------------
# the allow-list encodes what the incidents taught us
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "vtype,role",
    [
        # each of these shipped as a blank visual
        ("lineStackedColumnComboChart", "ColumnY"),
        ("lineStackedColumnComboChart", "LineY"),
        ("lineClusteredColumnComboChart", "LineY"),
        ("treemap", "Category"),
        ("gauge", "goal"),
    ],
)
def test_roles_behind_real_incidents_are_rejected(vtype, role):
    assert unsupported_roles(vtype, _qs(**{role: "Sales"})) == [role]


@pytest.mark.parametrize(
    "vtype,roles",
    [
        ("lineStackedColumnComboChart", ["Category", "Y", "Y2"]),
        ("clusteredBarChart", ["Category", "Y"]),
        ("treemap", ["Group", "Values"]),
        ("gauge", ["Y", "TargetValue"]),
        ("kpi", ["Indicator", "Goal", "TrendLine"]),
        ("cardVisual", ["Data"]),
        ("pivotTable", ["Rows", "Columns", "Values"]),
        ("slicer", ["Values"]),
        ("donutChart", ["Category", "Y"]),
    ],
)
def test_correct_role_sets_are_accepted(vtype, roles):
    assert unsupported_roles(vtype, _qs(**{r: "F" for r in roles})) == []


# ---------------------------------------------------------------------------
# the partial-knowledge contract — the part that keeps this usable
# ---------------------------------------------------------------------------


def test_an_unknown_visual_type_produces_no_findings():
    """Silence, not noise. A fetched report may hold any custom visual.

    Reporting unfamiliar types as broken would make the check worthless on
    exactly the reports where it is most needed.
    """
    assert known_roles("someCustomVisualByAVendor") is None
    assert unsupported_roles("someCustomVisualByAVendor", _qs(Whatever="F")) == []


def test_an_empty_role_stub_is_not_a_finding():
    """Desktop writes role keys with no projections all the time."""
    assert unsupported_roles("gauge", {"NotARole": {"projections": []}}) == []


def test_a_malformed_role_body_is_not_a_finding():
    assert unsupported_roles("gauge", {"Bogus": [{"properties": {}}]}) == []


# ---------------------------------------------------------------------------
# read-back: the output must distinguish working from blank
# ---------------------------------------------------------------------------


def _write(tmp_path, vtype, query_state):
    d = tmp_path / "visuals" / "v1"
    d.mkdir(parents=True)
    p = d / "visual.json"
    p.write_text(
        json.dumps(
            {
                "name": "v1",
                "visual": {"visualType": vtype, "query": {"queryState": query_state}},
            }
        ),
        encoding="utf-8",
    )
    return str(p)


def test_readback_names_the_role_not_just_the_field(tmp_path):
    """Reporting fields alone cannot tell a working visual from a blank one.

    This is why the old bindings tool never caught anything: it printed the
    field list, which is identical either way — only the role differs.
    """
    out = describe_query_state(
        _write(tmp_path, "lineStackedColumnComboChart", _qs(Y="Total Sales"))
    )
    assert "Y " in out or "Y<" in out or "Y  " in out
    assert "M[Total Sales]" in out


def test_readback_flags_a_dead_role_loudly(tmp_path):
    out = describe_query_state(
        _write(tmp_path, "lineStackedColumnComboChart", _qs(LineY="Sales Target"))
    )
    assert "!!" in out
    assert "LineY" in out
    assert "EMPTY" in out


def test_readback_stays_quiet_on_a_correct_visual(tmp_path):
    out = describe_query_state(
        _write(tmp_path, "lineStackedColumnComboChart", _qs(Category="D", Y="S", Y2="T"))
    )
    assert "!!" not in out


def test_readback_flags_a_data_visual_with_no_bindings(tmp_path):
    out = describe_query_state(_write(tmp_path, "clusteredBarChart", {}))
    assert "renders empty" in out


def test_readback_does_not_flag_a_textbox_for_having_no_bindings(tmp_path):
    out = describe_query_state(_write(tmp_path, "textbox", {}))
    assert "!!" not in out


def test_readback_reports_an_unreadable_file_rather_than_claiming_it_is_fine(tmp_path):
    p = tmp_path / "visuals" / "v1" / "visual.json"
    p.parent.mkdir(parents=True)
    p.write_text("{ not json", encoding="utf-8")
    assert "ERROR" in describe_query_state(str(p))
