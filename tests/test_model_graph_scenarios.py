"""Scenario matrix for the relationship-graph checks in the TMDL lint gate.

find_ambiguous_paths is the check that stops a model Power BI Desktop would
refuse to load, and find_orphan_tables is the one that catches a table nothing
can slice. Both walk a graph, so the interesting cases are shapes -- diamonds,
chains, cycles, self-loops, inactive edges -- not a single happy star schema.

Edge tuples are (from_table, to_table, is_active), matching what lint() builds
from a loaded PBIP reference.
"""

from __future__ import annotations

import pytest

from report.lint_pbip import find_ambiguous_paths, find_orphan_tables

# --- Shapes that are NOT ambiguous ------------------------------------------

UNAMBIGUOUS = [
    ("empty model", []),
    ("single edge", [("Sales", "DimDate", True)]),
    ("star", [("Sales", "DimDate", True), ("Sales", "DimProduct", True)]),
    ("chain", [("A", "B", True), ("B", "C", True)]),
    ("self loop", [("A", "A", True)]),
    ("two cycle", [("A", "B", True), ("B", "A", True)]),
    ("two facts one dim", [("A", "P", True), ("B", "P", True)]),
    (
        "snowflake",
        [("Sales", "DimProduct", True), ("DimProduct", "DimCategory", True)],
    ),
]


@pytest.mark.parametrize(
    "rels", [c[1] for c in UNAMBIGUOUS], ids=[c[0] for c in UNAMBIGUOUS]
)
def test_valid_shapes_report_no_ambiguity(rels):
    """A false positive here tells the user to inactivate a relationship their
    model needs."""
    assert find_ambiguous_paths(rels) == []


def test_a_cycle_does_not_hang_the_walk():
    """reachable() is a BFS over user-supplied edges; a cycle must terminate."""
    rels = [("A", "B", True), ("B", "C", True), ("C", "A", True)]
    assert find_ambiguous_paths(rels) == []


# --- Shapes that ARE ambiguous ----------------------------------------------


def test_the_classic_diamond_is_caught():
    """Sales reaches Shared through both DimA and DimB -- Desktop rejects it."""
    rels = [
        ("Sales", "DimA", True),
        ("Sales", "DimB", True),
        ("DimA", "Shared", True),
        ("DimB", "Shared", True),
    ]
    found = find_ambiguous_paths(rels)
    assert len(found) == 1
    hub, target, via_a, via_b = found[0]
    assert (hub, target) == ("Shared", "Sales")
    assert {via_a, via_b} == {"DimA", "DimB"}


def test_inactivating_one_leg_resolves_the_diamond():
    """This is the fix the lint message tells the user to apply, so it has to
    actually clear the finding."""
    rels = [
        ("Sales", "DimA", True),
        ("Sales", "DimB", True),
        ("DimA", "Shared", True),
        ("DimB", "Shared", False),
    ]
    assert find_ambiguous_paths(rels) == []


def test_ambiguity_through_a_longer_leg_is_caught():
    """The two paths need not be the same length -- one hop versus three."""
    rels = [
        ("Sales", "C1", True),
        ("Sales", "C2", True),
        ("C1", "X", True),
        ("X", "Shared", True),
        ("C2", "Shared", True),
    ]
    found = find_ambiguous_paths(rels)
    assert len(found) == 1
    assert found[0][:2] == ("Shared", "Sales")


def test_each_hub_target_pair_is_reported_once():
    """Three children all reaching the same table is three PAIRS but one
    problem; reporting it three times buries the rest of the lint output."""
    rels = [
        ("S", "A", True),
        ("S", "B", True),
        ("S", "C", True),
        ("A", "Z", True),
        ("B", "Z", True),
        ("C", "Z", True),
    ]
    assert len(find_ambiguous_paths(rels)) == 1


def test_two_separate_ambiguities_are_both_reported():
    rels = [
        ("S", "A", True),
        ("S", "B", True),
        ("A", "Z", True),
        ("B", "Z", True),
        ("A", "Y", True),
        ("B", "Y", True),
    ]
    found = find_ambiguous_paths(rels)
    assert {f[1] for f in found} == {"S"}
    assert {f[0] for f in found} == {"Y", "Z"}


def test_the_result_is_deterministic():
    """Lint output feeds a diff-able report; set iteration order must not leak
    into it."""
    rels = [
        ("Sales", "DimB", True),
        ("Sales", "DimA", True),
        ("DimB", "Shared", True),
        ("DimA", "Shared", True),
    ]
    runs = [find_ambiguous_paths(rels) for _ in range(5)]
    assert all(r == runs[0] for r in runs)
    assert runs[0][0][2:] == ("DimA", "DimB")


def test_inactive_edges_are_ignored_entirely():
    rels = [
        ("Sales", "DimA", False),
        ("Sales", "DimB", False),
        ("DimA", "Shared", False),
        ("DimB", "Shared", False),
    ]
    assert find_ambiguous_paths(rels) == []


# --- Orphan tables ----------------------------------------------------------


def test_a_table_in_an_active_relationship_is_not_an_orphan():
    assert find_orphan_tables(["Sales", "DimDate"], [("Sales", "DimDate", True)]) == []


def test_being_on_either_end_counts_as_referenced():
    assert find_orphan_tables(["A", "B", "C"], [("A", "B", True)]) == ["C"]


def test_a_table_with_only_inactive_relationships_is_an_orphan():
    """Inactive relationships do not filter anything, so the table is still
    unreachable from a visual."""
    assert find_orphan_tables(["A", "B"], [("A", "B", False)]) == ["A", "B"]


@pytest.mark.parametrize(
    "name", ["DimDate", "dimdate", "DIMDATE", "DateTable", "datetable", "dateTABLE"]
)
def test_date_tables_are_exempt_in_any_casing(name):
    """A marked date table is legitimately unrelated in some models; flagging
    it every run trains the user to ignore the warning."""
    assert find_orphan_tables([name], []) == []


@pytest.mark.parametrize("name", ["_staging", "_Measures", "__internal"])
def test_underscore_prefixed_tables_are_exempt(name):
    """The leading underscore is the convention for a measure/helper table."""
    assert find_orphan_tables([name], []) == []


def test_a_similar_name_is_not_exempt():
    """The exemption is exact, not a prefix match -- 'DimDateExtended' is a
    real table and a real orphan."""
    assert find_orphan_tables(["DimDateExtended"], []) == ["DimDateExtended"]


def test_orphans_come_back_sorted():
    assert find_orphan_tables(["zz", "aa", "mm"], []) == ["aa", "mm", "zz"]


@pytest.mark.parametrize(
    "tables,rels",
    [([], []), ([], [("A", "B", True)]), (["A"], [])],
    ids=["nothing", "edges but no tables", "tables but no edges"],
)
def test_degenerate_inputs_do_not_raise(tables, rels):
    result = find_orphan_tables(tables, rels)
    assert isinstance(result, list)


def test_an_edge_naming_a_table_that_does_not_exist_is_harmless():
    """Relationships and the table list come from different parts of the PBIP;
    a dangling edge must not crash the lint run."""
    assert find_orphan_tables(["A"], [("A", "GhostTable", True)]) == []
