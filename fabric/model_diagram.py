from __future__ import annotations

_ONE = {"1", "one"}
_MANY = {"2", "many"}
_BOTH = {"2", "both", "bothdirections"}


def _cardinality(raw: str) -> str:
    value = (raw or "").strip().lower()
    if value in _ONE:
        return "1"
    if value in _MANY:
        return "*"
    return "?"


def _bidirectional(raw: str) -> bool:
    return (raw or "").strip().lower() in _BOTH


def _role(table: str, columns: list, measures: list, from_side: set, to_side: set) -> str:
    if not columns and measures:
        return "measures"
    if table in from_side:
        return "fact"
    if table in to_side:
        return "dimension"
    return "standalone"


def build_model_diagram(model: str, meta: dict) -> dict:
    tables = list(meta.get("tables") or [])
    visible = set(tables)
    row_counts = meta.get("row_counts") or {}
    columns_by_table = meta.get("columns_by_table") or {}
    measures_by_table = meta.get("measures_by_table") or {}
    all_relationships = meta.get("relationships") or []

    # A relationship to a table the reader already filtered out is the auto
    # date hierarchy noise -- counted so the diagram can say it left it out,
    # rather than drawing edges into tables that are not on the canvas.
    relationships = [
        r
        for r in all_relationships
        if r.get("from_table") in visible and r.get("to_table") in visible
    ]

    from_side = {r["from_table"] for r in relationships if r.get("is_active")}
    to_side = {r["to_table"] for r in relationships if r.get("is_active")}
    join_columns: dict[str, set] = {}
    for r in relationships:
        join_columns.setdefault(r["from_table"], set()).add(r.get("from_column"))
        join_columns.setdefault(r["to_table"], set()).add(r.get("to_column"))

    nodes = []
    for table in tables:
        columns = columns_by_table.get(table) or []
        measures = measures_by_table.get(table) or []
        keys = join_columns.get(table, set())
        rows = row_counts.get(table)
        nodes.append(
            {
                "id": table,
                "rows": rows if isinstance(rows, int) else None,
                "role": _role(table, columns, measures, from_side, to_side),
                "measures": len(measures),
                "columns": [
                    {"name": str(c), "type": str(t or ""), "key": c in keys}
                    for c, t in columns
                ],
            }
        )

    edges = [
        {
            "id": f"{r['from_table']}.{r['from_column']}->{r['to_table']}.{r['to_column']}",
            "from": r["from_table"],
            "fromColumn": r.get("from_column", ""),
            "to": r["to_table"],
            "toColumn": r.get("to_column", ""),
            "active": bool(r.get("is_active")),
            "bidirectional": _bidirectional(r.get("cross_filter", "")),
            "cardinality": (
                f"{_cardinality(r.get('from_cardinality', ''))}"
                f":{_cardinality(r.get('to_cardinality', ''))}"
            ),
        }
        for r in relationships
    ]

    return {
        "model": model,
        "nodes": nodes,
        "edges": edges,
        "hiddenEdges": len(all_relationships) - len(relationships),
    }
