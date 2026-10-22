"""queryState repair after a bind — the three ways a visual goes silently blank.

Power BI Desktop does not reject a malformed `queryState`; it renders an empty
visual and says nothing. Every defect pinned here therefore produced a report
that *looked* built — the bind tool reported success each time — and only
showed up when a human opened the .pbip and saw an empty box.

The originating incident: a combo chart was re-bound 13 times over five hours
chasing a line series that never appeared, because pbi_cli writes the line
measure to a role name Power BI has never had.
"""

from __future__ import annotations

from llm.pbi_tool_exec import normalize_pbir_query_state


def _proj(entity: str, prop: str, kind: str = "Measure") -> dict:
    return {
        "field": {kind: {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}},
        "queryRef": f"{entity}.{prop}",
        "nativeQueryRef": prop,
    }


def _visual(vtype: str, query_state: dict) -> dict:
    return {
        "name": "v1",
        "visual": {"visualType": vtype, "query": {"queryState": query_state}},
    }


def _roles(vdata: dict) -> dict[str, list[str]]:
    qs = vdata["visual"]["query"]["queryState"]
    return {
        r: [p["queryRef"] for p in b.get("projections", [])]
        for r, b in qs.items()
        # A non-dict body is exactly the malformed shape the repair has to
        # survive, so the helper has to survive reading it too.
        if isinstance(b, dict)
    }


# ---------------------------------------------------------------------------
# 1. pbi_cli's invented combo-chart roles
# ---------------------------------------------------------------------------


def test_combo_chart_column_and_line_roles_are_renamed_to_y_and_y2():
    """ColumnY/LineY are not Power BI roles; Y/Y2 are.

    pbi_cli's ROLE_ALIASES maps "column"/"value" -> "ColumnY" and
    "line" -> "LineY". Desktop drops both, so the chart renders with a
    populated X-axis and two empty value buckets.
    """
    vdata = _visual(
        "lineStackedColumnComboChart",
        {
            "Category": {"projections": [_proj("Date_Table", "Month Year", "Column")]},
            "ColumnY": {"projections": [_proj("Measures_Table", "Total Sales")]},
            "LineY": {"projections": [_proj("Measures_Table", "Sales Target")]},
        },
    )
    assert normalize_pbir_query_state(vdata) is True
    assert _roles(vdata) == {
        "Category": ["Date_Table.Month Year"],
        "Y": ["Measures_Table.Total Sales"],
        "Y2": ["Measures_Table.Sales Target"],
    }


def test_clustered_variant_of_the_combo_chart_is_repaired_too():
    vdata = _visual(
        "lineClusteredColumnComboChart",
        {"ColumnY": {"projections": [_proj("M", "Sales")]}},
    )
    assert normalize_pbir_query_state(vdata) is True
    assert "Y" in _roles(vdata) and "ColumnY" not in _roles(vdata)


def test_a_partially_migrated_combo_chart_merges_rather_than_overwrites():
    """Y already holding data must not be clobbered by the ColumnY rename.

    A visual bound both before and after the fix has measures in both roles;
    dropping either one loses a series the user asked for.
    """
    vdata = _visual(
        "lineStackedColumnComboChart",
        {
            "Y": {"projections": [_proj("M", "Already Correct")]},
            "ColumnY": {"projections": [_proj("M", "Legacy Bound")]},
        },
    )
    assert normalize_pbir_query_state(vdata) is True
    assert _roles(vdata)["Y"] == ["M.Already Correct", "M.Legacy Bound"]


def test_a_non_combo_chart_keeps_its_own_roles():
    """The rename is keyed on visualType — a bar chart's Y is already right."""
    vdata = _visual(
        "clusteredBarChart",
        {
            "Category": {"projections": [_proj("S", "Region", "Column")]},
            "Y": {"projections": [_proj("M", "Total Sales")]},
        },
    )
    assert normalize_pbir_query_state(vdata) is False


# ---------------------------------------------------------------------------
# 2. unmapped aliases leaking the caller's own parameter name
# ---------------------------------------------------------------------------


def test_gauge_target_is_recovered_from_the_dead_lowercase_goal_role():
    """`goal` is not in pbi_cli's gauge alias map, so it is written verbatim.

    The gauge then renders with no target at all — Desktop falls back to a
    max of 2x the value, which is what made a 207M gauge show a 414M range.
    """
    vdata = _visual(
        "gauge",
        {
            "Y": {"projections": [_proj("M", "Total Sales")]},
            "MaxValue": {"projections": []},
            "goal": {"projections": [_proj("M", "Sales Target")]},
        },
    )
    assert normalize_pbir_query_state(vdata) is True
    roles = _roles(vdata)
    assert roles["TargetValue"] == ["M.Sales Target"]
    assert "goal" not in roles


def test_a_junk_role_duplicating_a_real_one_is_dropped():
    """`field` mirrors Category on charts bound via the field= parameter."""
    vdata = _visual(
        "donutChart",
        {
            "Category": {"projections": [_proj("S", "Category", "Column")]},
            "Y": {"projections": [_proj("M", "Total Sales")]},
            "field": {"projections": [_proj("S", "Category", "Column")]},
        },
    )
    assert normalize_pbir_query_state(vdata) is True
    assert "field" not in _roles(vdata)
    assert _roles(vdata)["Category"] == ["S.Category"]


def test_a_junk_role_holding_the_only_copy_of_a_field_is_kept():
    """Dropping it would destroy a binding no other role has.

    Deleting unknown roles wholesale would turn a recoverable mistake into
    silent data loss, so uniqueness is the gate — not the role name.
    """
    vdata = _visual(
        "clusteredBarChart",
        {
            "Category": {"projections": [_proj("S", "Region", "Column")]},
            "mystery": {"projections": [_proj("M", "Unique Measure")]},
        },
    )
    normalize_pbir_query_state(vdata)
    assert _roles(vdata)["mystery"] == ["M.Unique Measure"]


def test_pascal_case_roles_are_never_treated_as_junk():
    """Every real PBIR role is PascalCase; the junk heuristic keys on that."""
    vdata = _visual(
        "kpi",
        {
            "Indicator": {"projections": [_proj("M", "Total Sales")]},
            "Goal": {"projections": [_proj("M", "Sales Target")]},
            "TrendLine": {"projections": [_proj("D", "Month Year", "Column")]},
        },
    )
    assert normalize_pbir_query_state(vdata) is False


# ---------------------------------------------------------------------------
# 3. duplicate projections from repeated re-binds
# ---------------------------------------------------------------------------


def test_repeated_binds_collapse_to_one_projection_per_field():
    """A card expects a single scalar; 3 copies of one measure blanks it."""
    vdata = _visual(
        "cardVisual",
        {"Data": {"projections": [_proj("M", "Total Sales")] * 3}},
    )
    assert normalize_pbir_query_state(vdata) is True
    assert _roles(vdata)["Data"] == ["M.Total Sales"]


def test_genuinely_distinct_fields_in_one_role_are_preserved():
    """Dedup is by queryRef — a matrix legitimately holds several measures."""
    vdata = _visual(
        "pivotTable",
        {
            "Rows": {"projections": [_proj("D", "Month Year", "Column")]},
            "Values": {
                "projections": [
                    _proj("M", "Total Sales"),
                    _proj("M", "Sales Target"),
                    _proj("M", "Target Achievement %"),
                ]
            },
        },
    )
    assert normalize_pbir_query_state(vdata) is False
    assert len(_roles(vdata)["Values"]) == 3


def test_normalization_is_idempotent():
    """The repair runs after every bind; a second pass must be a no-op."""
    vdata = _visual(
        "lineStackedColumnComboChart",
        {
            "Category": {"projections": [_proj("D", "Month Year", "Column")] * 2},
            "ColumnY": {"projections": [_proj("M", "Total Sales")]},
            "LineY": {"projections": [_proj("M", "Sales Target")]},
            "field": {"projections": [_proj("D", "Month Year", "Column")]},
        },
    )
    assert normalize_pbir_query_state(vdata) is True
    first = _roles(vdata)
    assert normalize_pbir_query_state(vdata) is False
    assert _roles(vdata) == first


# ---------------------------------------------------------------------------
# shape guards — the original bug was a shape confusion that raised and was
# then swallowed by a bare except, disabling the repair entirely
# ---------------------------------------------------------------------------


def test_a_visual_with_no_query_state_is_left_alone():
    assert normalize_pbir_query_state({"visual": {"visualType": "textbox"}}) is False


def test_a_malformed_role_body_does_not_raise():
    """visual.objects is a LIST of style dicts; queryState roles are dicts.

    Confusing the two is what raised AttributeError and, via a bare
    `except: pass`, silently switched the whole repair off.
    """
    vdata = _visual(
        "clusteredBarChart",
        {
            "Category": {"projections": [_proj("S", "Region", "Column")]},
            "Bogus": [{"properties": {}}],
        },
    )
    normalize_pbir_query_state(vdata)  # must not raise
    assert _roles(vdata)["Category"] == ["S.Region"]


# ---------------------------------------------------------------------------
# wiring: the repair has to actually RUN, not merely exist
# ---------------------------------------------------------------------------


def test_treemap_groups_on_group_not_category():
    """pbi_cli aliases treemap `category` to Category; treemaps use Group.

    Confirmed against the two Desktop-authored treemaps in the MavenMarket
    report, both Group/Values. Bound to Category the grouping field is
    dropped and the treemap renders as a single undifferentiated block.
    """
    vdata = _visual(
        "treemap",
        {
            "Category": {"projections": [_proj("S", "Category", "Column")]},
            "Values": {"projections": [_proj("M", "Total Sales")]},
        },
    )
    assert normalize_pbir_query_state(vdata) is True
    roles = _roles(vdata)
    assert roles["Group"] == ["S.Category"]
    assert "Category" not in roles


def test_the_repair_is_reachable_from_the_bind_branch():
    """Regression: the repair once sat AFTER the dispatcher's if/elif chain.

    `pbi_visual_bind` returns from inside its own branch, so the block was
    unreachable for the only tool it targeted — the role fix and the
    projection dedup both silently did nothing while their unit tests passed,
    because those tests call the function directly. This asserts the call site
    is lexically inside the branch that returns before the chain ends.
    """
    import ast
    import inspect

    import llm.pbi_tool_exec as mod

    tree = ast.parse(inspect.getsource(mod))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_execute_pbi_tool_impl"
    )

    # More than one `if` mentions the bind tool — there is an early validation
    # guard as well as the handler — so check every one of them and require the
    # call to sit inside at least one. What must never happen again is the call
    # living outside all of them, after a chain that has already returned.
    bind_branches = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.If)
        and any(
            isinstance(const, ast.Constant) and const.value == "pbi_visual_bind"
            for const in ast.walk(node.test)
        )
    ]
    assert bind_branches, "no pbi_visual_bind dispatch branch found"

    calls_inside = [
        c
        for branch in bind_branches
        for stmt in branch.body
        for c in ast.walk(stmt)
        if isinstance(c, ast.Call)
        and isinstance(c.func, ast.Name)
        and c.func.id == "normalize_pbir_query_state"
    ]
    assert calls_inside, (
        "normalize_pbir_query_state is not called inside the pbi_visual_bind "
        "branch body — if it sits after the if/elif chain it can never run, "
        "because the branch returns first."
    )
