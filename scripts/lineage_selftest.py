"""Offline self-test for the tenant lineage graph and impact analysis.

The Scanner API needs a tenant-admin setting that is usually not on yet, so
none of this logic could be exercised against a live tenant on day one. This
script feeds a synthetic scanResult payload -- shaped exactly like the real
one -- through ingest, transitive closure, surface diffing, DAX matching and
alert rendering, so the engine is provable before the permission lands.

    python scripts/lineage_selftest.py

Exits non-zero on the first failure. Touches no network and no tenant.
"""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fabric import impact as IM
from fabric import lineage as L

_checks = 0
_failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    global _checks
    _checks += 1
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}" + (f"  <- {detail}" if detail else ""))
        _failures.append(label)


DS1 = "11111111-1111-1111-1111-111111111111"
DS2 = "22222222-2222-2222-2222-222222222222"
R1 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
R2 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
R3 = "cccccccc-cccc-cccc-cccc-cccccccccccc"
D1 = "dddddddd-dddd-dddd-dddd-dddddddddddd"
WS = "99999999-9999-9999-9999-999999999999"


def sample_workspace() -> dict:
    """A workspace shaped like a real Scanner API scanResult entry."""
    return {
        "id": WS,
        "name": "Finance",
        "state": "Active",
        "datasets": [
            {
                "id": DS1,
                "name": "Sales Model",
                "tables": [
                    {
                        "name": "Sales",
                        "columns": [
                            {"name": "Amount", "dataType": "Decimal"},
                            {"name": "Region", "dataType": "String"},
                            {"name": "OrderDate", "dataType": "DateTime"},
                        ],
                        "measures": [
                            {
                                "name": "Total Revenue",
                                "expression": "SUM(Sales[Amount])",
                            }
                        ],
                    }
                ],
            },
            {
                "id": DS2,
                "name": "Sales Chained",
                "tables": [
                    {
                        "name": "Roll",
                        "columns": [{"name": "Bucket", "dataType": "String"}],
                        "measures": [
                            {
                                "name": "Revenue YoY",
                                "expression": (
                                    "DIVIDE([Total Revenue], "
                                    "CALCULATE([Total Revenue], "
                                    "DATEADD(Sales[OrderDate], -1, YEAR)))"
                                ),
                            },
                            {
                                "name": "Bucket Count",
                                "expression": "COUNTROWS(Roll)",
                            },
                        ],
                    }
                ],
                "upstreamDatasets": [{"targetDatasetId": DS1}],
            },
        ],
        "reports": [
            {"id": R1, "name": "Exec Summary", "datasetId": DS1},
            {"id": R2, "name": "Ops Detail", "datasetId": DS1},
            {"id": R3, "name": "Chained Report", "datasetId": DS2},
        ],
        "dashboards": [
            {
                "id": D1,
                "displayName": "CFO Dash",
                "tiles": [{"id": "t1", "title": "Revenue", "datasetId": DS1}],
            }
        ],
    }


def pbir_definition(entity: str, prop: str, kind: str = "Measure") -> dict:
    """A getDefinition payload carrying one visual bound to one field."""
    visual = {
        "name": "revenueCard",
        "visual": {
            "visualType": "card",
            "query": {
                "queryState": {
                    "Values": {
                        "projections": [
                            {
                                "field": {
                                    kind: {
                                        "Expression": {"SourceRef": {"Entity": entity}},
                                        "Property": prop,
                                    }
                                }
                            }
                        ]
                    }
                }
            },
        },
    }
    payload = base64.b64encode(json.dumps(visual).encode("utf-8")).decode("ascii")
    return {
        "definition": {
            "parts": [
                {
                    "path": "definition/pages/Summary/visuals/revenueCard/visual.json",
                    "payload": payload,
                    "payloadType": "InlineBase64",
                }
            ]
        }
    }


def test_ingest(graph: L.LineageGraph) -> None:
    print("\ningest")
    check(
        "all six artifacts became nodes", len(graph.nodes) == 6, str(len(graph.nodes))
    )
    check("dataset schema captured", "Sales[Amount]" in graph.nodes[DS1].columns)
    check("measure expression captured", bool(graph.nodes[DS2].measures))
    check("report binds to its dataset", graph.nodes[R1].binds_to == DS1)
    check("dashboard tile recorded", graph.nodes[D1].tiles == ["Revenue"])
    check("workspace name on node", graph.nodes[DS1].workspace_name == "Finance")


def test_edges(graph: L.LineageGraph) -> None:
    print("\nedges and transitive closure")
    down = {n.id: d for n, d in graph.downstream_of(DS1)}
    check("direct report is depth 1", down.get(R1) == 1, str(down.get(R1)))
    check("dashboard is downstream", D1 in down)
    check("chained dataset is downstream", down.get(DS2) == 1, str(down.get(DS2)))
    check(
        "report on chained dataset is depth 2",
        down.get(R3) == 2,
        f"got {down.get(R3)}",
    )
    check("changed model excludes itself", DS1 not in down)
    check("unrelated direction is empty", graph.downstream_of(R1) == [])


def test_surface_and_diff(graph: L.LineageGraph) -> None:
    print("\nsurface diff")
    before = IM.ModelSurface.from_lineage_node(graph.nodes[DS1])
    check("surface has 3 columns", len(before.columns) == 3, str(before.columns))
    check("surface has the measure", "Sales[Total Revenue]" in before.measures)

    after = IM.ModelSurface(
        tables=set(before.tables),
        columns={k: v for k, v in before.columns.items() if k != "Sales[Region]"},
        measures=set(),
    )
    diff = IM.diff_surface(before, after)
    check("dropped measure detected", diff.removed_measures == ["Sales[Total Revenue]"])
    check("dropped column detected", diff.removed_columns == ["Sales[Region]"])
    check("breaking refs merged", len(diff.breaking_refs) == 2, str(diff.breaking_refs))
    check("diff is not empty", not diff.is_empty())

    same = IM.diff_surface(before, before)
    check("identical surfaces diff clean", same.is_empty())

    retyped_after = IM.ModelSurface(
        tables=set(before.tables),
        columns={**before.columns, "Sales[Amount]": "String"},
        measures=set(before.measures),
    )
    rt = IM.diff_surface(before, retyped_after)
    check(
        "retype flagged as breaking",
        rt.retyped_columns == [("Sales[Amount]", "Decimal", "String")],
        str(rt.retyped_columns),
    )
    check("retype lands in breaking refs", "Sales[Amount]" in rt.breaking_refs)

    renamed_after = IM.ModelSurface(
        tables=set(before.tables),
        columns=before.columns,
        measures={"Sales[Revenue Total]"},
    )
    rn = IM.diff_surface(before, renamed_after)
    check(
        "rename candidate paired",
        rn.rename_candidates() == [("Sales[Total Revenue]", "Sales[Revenue Total]")],
        str(rn.rename_candidates()),
    )


def test_xmla_surface() -> None:
    print("\nXMLA surface adapter")
    meta = {
        "tables": ["Sales"],
        "columns_by_table": {"Sales": [("Amount", "Decimal"), ("Region", "String")]},
        "measures_by_table": {"Sales": ["Total Revenue"]},
    }
    s = IM.ModelSurface.from_xmla_metadata(meta)
    check(
        "columns lifted from XMLA",
        s.columns
        == {
            "Sales[Amount]": "Decimal",
            "Sales[Region]": "String",
        },
        str(s.columns),
    )
    check("measures lifted from XMLA", s.measures == {"Sales[Total Revenue]"})
    check(
        "XMLA and scanner surfaces agree",
        s.measures
        == IM.ModelSurface.from_lineage_node(
            L.LineageNode(
                id="x",
                name="x",
                kind=L.DATASET,
                measures={"Sales[Total Revenue]": ""},
            )
        ).measures,
    )


def test_dax_matching(graph: L.LineageGraph) -> None:
    print("\nchained-dataset DAX matching")
    hit = IM._match_chained_dataset(graph.nodes[DS2], {"Sales[Total Revenue]"}, depth=1)
    check("bare [Measure] reference matched", hit is not None)
    check(
        "only the referencing measure is named",
        hit is not None and len(hit.details) == 1,
        str(hit.details if hit else None),
    )
    check(
        "unrelated measure not flagged",
        hit is not None and "Bucket Count" not in " ".join(hit.details),
    )
    qualified = IM._match_chained_dataset(
        graph.nodes[DS2], {"Sales[OrderDate]"}, depth=1
    )
    check("qualified Table[Column] reference matched", qualified is not None)
    miss = IM._match_chained_dataset(graph.nodes[DS2], {"Sales[Nonexistent]"}, depth=1)
    check("no false positive on absent field", miss is None)


def test_report_bindings() -> None:
    print("\nreport definition parsing")
    binds = IM._bindings_from_definition(pbir_definition("Sales", "Total Revenue"))
    check("one binding parsed", len(binds) == 1, str(binds))
    check("page name from path", bool(binds) and binds[0][0] == "Summary")
    check("visual name from json", bool(binds) and binds[0][1] == "revenueCard")
    check("field ref normalised", bool(binds) and binds[0][2] == "Sales[Total Revenue]")
    check("empty payload yields nothing", IM._bindings_from_definition({}) == [])


def test_analyze_shallow(graph: L.LineageGraph) -> None:
    print("\nanalyze (deep=False -- reports must be unchecked, never cleared)")
    before = IM.ModelSurface.from_lineage_node(graph.nodes[DS1])
    after = IM.ModelSurface(tables=set(before.tables), columns=before.columns)
    diff = IM.diff_surface(before, after)
    rep = IM.analyze(graph, DS1, diff, deep=False)

    kinds = {h.node.id: h.confidence for h in rep.hits}
    check("chained dataset confirmed broken", kinds.get(DS2) == IM.CONFIRMED)
    check("dashboard flagged for human check", kinds.get(D1) == IM.POTENTIAL)
    check("no report silently cleared", R1 not in kinds and R2 not in kinds)
    check(
        "both direct reports listed as unchecked",
        sum("Exec Summary" in u or "Ops Detail" in u for u in rep.unchecked) == 2,
        str(rep.unchecked),
    )
    check(
        "empty diff produces no hits", not IM.analyze(graph, DS1, IM.SurfaceDiff()).hits
    )


def test_analyze_deep(graph: L.LineageGraph, monkeypatched: dict) -> None:
    print("\nanalyze (deep=True, definitions stubbed)")
    before = IM.ModelSurface.from_lineage_node(graph.nodes[DS1])
    after = IM.ModelSurface(tables=set(before.tables), columns=before.columns)
    diff = IM.diff_surface(before, after)
    rep = IM.analyze(graph, DS1, diff, deep=True)

    by_id = {h.node.id: h for h in rep.hits}
    check(
        "bound report confirmed broken",
        by_id.get(R1, None) is not None and by_id[R1].confidence == IM.CONFIRMED,
    )
    check(
        "visual named in detail",
        R1 in by_id and "revenueCard" in " ".join(by_id[R1].details),
        str(by_id[R1].details if R1 in by_id else None),
    )
    check("clean report omitted entirely", R2 not in by_id)
    check(
        "clean report not in unchecked either",
        not any("Ops Detail" in u for u in rep.unchecked),
        str(rep.unchecked),
    )
    check(
        "unreadable report lands in unchecked",
        any("Chained Report" in u for u in rep.unchecked),
        str(rep.unchecked),
    )
    check("confirmed sorts before potential", rep.hits[0].confidence == IM.CONFIRMED)

    alert = IM.format_alert(rep)
    check("alert names the breakage", "WILL BREAK" in alert)
    check("alert names the model", "Sales Model" in alert)
    check("alert surfaces unchecked items", "NOT CHECKED" in alert)
    check("alert states scan age", "lineage scan" in alert)
    check("known scan age rendered sanely", "unknown age" not in alert)
    check("alert lists the dropped measure", "Sales[Total Revenue]" in alert)

    never_scanned = L.LineageGraph(nodes=dict(graph.nodes), edges=dict(graph.edges))
    stale = IM.analyze(never_scanned, DS1, diff, deep=False)
    check(
        "never-scanned graph admits unknown age",
        "unknown age" in IM.format_alert(stale),
    )
    print("\n" + "-" * 68)
    print(alert)
    print("-" * 68)

    clean = IM.format_alert(IM.analyze(graph, DS1, IM.SurfaceDiff()))
    check("no-op change reads as safe", "No breaking change detected" in clean)


def test_cache_roundtrip(graph: L.LineageGraph) -> None:
    print("\ncache serialisation")
    restored = L.LineageGraph.from_dict(json.loads(json.dumps(graph.to_dict())))
    check("node count survives round trip", len(restored.nodes) == len(graph.nodes))
    check(
        "edges survive round trip",
        bool(restored.downstream_of(DS1))
        and {n.id for n, _ in restored.downstream_of(DS1)}
        == {n.id for n, _ in graph.downstream_of(DS1)},
    )
    check(
        "schema survives round trip",
        restored.nodes[DS1].columns == graph.nodes[DS1].columns,
    )


def test_gap_reporting() -> None:
    print("\ncoverage gap reporting")
    g = L.LineageGraph()
    L._ingest_workspace(
        {"id": "w", "name": "Archived", "state": "Deleted", "datasets": []}, g
    )
    check("inactive workspace recorded as a gap", len(g.gaps) == 1, str(g.gaps))
    g2 = L.LineageGraph()
    L._ingest_workspace(
        {
            "id": "w",
            "name": "Odd",
            "state": "Active",
            "datasets": [{"id": "d", "name": "No Schema"}],
            "reports": [{"id": "r", "name": "Orphan"}],
        },
        g2,
    )
    check(
        "schema-less dataset flagged",
        any("no schema" in x for x in g2.gaps),
        str(g2.gaps),
    )
    check(
        "unbound report flagged",
        any("no bound dataset" in x for x in g2.gaps),
        str(g2.gaps),
    )
    check("summarize surfaces gaps", "Coverage gaps" in L.summarize(g2))


def main() -> int:
    graph = L.LineageGraph(scanned_at=time.time())
    L._ingest_workspace(sample_workspace(), graph)
    graph.workspace_count = 1

    test_ingest(graph)
    test_edges(graph)
    test_surface_and_diff(graph)
    test_xmla_surface()
    test_dax_matching(graph)
    test_report_bindings()
    test_analyze_shallow(graph)

    stub = {
        R1: pbir_definition("Sales", "Total Revenue"),
        R2: pbir_definition("Sales", "Amount"),
        R3: None,
    }
    original = IM._fetch_report_definition
    IM._fetch_report_definition = lambda workspace_id, report_id: stub.get(report_id)
    try:
        test_analyze_deep(graph, stub)
    finally:
        IM._fetch_report_definition = original

    test_cache_roundtrip(graph)
    test_gap_reporting()

    print(f"\n{_checks - len(_failures)}/{_checks} checks passed")
    if _failures:
        print("failed: " + ", ".join(_failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
