from __future__ import annotations

from dataclasses import dataclass

from .plan import ColumnSpec, SchemaPlan, TableSpec, resolve_source_key

_SQL_TYPE_TO_TMDL: dict[str, str] = {
    "int": "int64",
    "bigint": "int64",
    "smallint": "int64",
    "tinyint": "int64",
    "bit": "boolean",
    "decimal": "double",
    "numeric": "double",
    "money": "double",
    "smallmoney": "double",
    "float": "double",
    "real": "double",
    "double": "double",
    "date": "dateTime",
    "datetime": "dateTime",
    "datetime2": "dateTime",
    "smalldatetime": "dateTime",
    "datetimeoffset": "dateTime",
    "time": "dateTime",
    "varchar": "string",
    "nvarchar": "string",
    "char": "string",
    "nchar": "string",
    "text": "string",
    "ntext": "string",
    "uniqueidentifier": "string",
}


@dataclass
class RepairAction:
    severity: str
    where: str
    message: str


def _sql_to_tmdl(sql_type: str) -> str:
    return _SQL_TYPE_TO_TMDL.get((sql_type or "").lower(), "string")


def auto_repair_plan(
    plan: SchemaPlan,
    schema_tables: dict[str, list[tuple[str, str]]],
) -> tuple[SchemaPlan, list[RepairAction]]:
    actions: list[RepairAction] = []

    def _plan_table(name: str) -> TableSpec | None:
        for t in plan.tables:
            if t.name == name:
                return t
        return None

    def _has_column(t: TableSpec, col_name: str) -> bool:
        return any(c.name == col_name for c in t.columns)

    def _add_column_from_schema(t: TableSpec, col_name: str, where: str) -> bool:
        sch_cols = schema_tables.get(resolve_source_key(t, schema_tables), [])
        for sql_col, sql_type in sch_cols:
            if sql_col == col_name:
                t.columns.append(
                    ColumnSpec(
                        name=col_name,
                        source_column=col_name,
                        data_type=_sql_to_tmdl(sql_type),
                        is_hidden=True,
                        is_key=False,
                        summarize_by="none",
                    )
                )
                actions.append(
                    RepairAction(
                        severity="info",
                        where=where,
                        message=f"Auto-added missing column '{col_name}' to '{t.name}' "
                        f"(found in source schema, hidden by default)",
                    )
                )
                return True
        return False

    for t in plan.tables:
        if t.kind == "calculated":
            continue
        sch_cols = {
            sql_col
            for sql_col, _ in schema_tables.get(
                resolve_source_key(t, schema_tables), []
            )
        }
        if not sch_cols:
            continue
        surviving_cols: list[ColumnSpec] = []
        for c in t.columns:
            if c.source_column in sch_cols:
                surviving_cols.append(c)
            else:
                actions.append(
                    RepairAction(
                        severity="info",
                        where=f"tables ({t.name}).columns ({c.name})",
                        message=f"Dropping column '{c.name}' — source_column "
                        f"'{c.source_column}' no longer exists in source "
                        f"table '{t.name}' (schema changed since plan was "
                        f"generated, e.g. PK added)",
                    )
                )
        if len(surviving_cols) != len(t.columns):
            t.columns = surviving_cols

    surviving_rels = []
    for i, r in enumerate(plan.relationships):
        rel_where = f"relationships[{i}] ({r.name})"

        from_tbl = _plan_table(r.from_table)
        if from_tbl is None:
            actions.append(
                RepairAction(
                    severity="warning",
                    where=rel_where,
                    message=f"Dropping relationship — from_table '{r.from_table}' "
                    f"is not in the plan and could not be auto-added",
                )
            )
            continue
        if not _has_column(from_tbl, r.from_column):
            added = _add_column_from_schema(from_tbl, r.from_column, rel_where)
            if not added:
                actions.append(
                    RepairAction(
                        severity="warning",
                        where=rel_where,
                        message=f"Dropping relationship — column '{r.from_column}' "
                        f"does not exist in source table '{from_tbl.name}'",
                    )
                )
                continue

        to_tbl = _plan_table(r.to_table)
        if to_tbl is None:
            actions.append(
                RepairAction(
                    severity="warning",
                    where=rel_where,
                    message=f"Dropping relationship — to_table '{r.to_table}' "
                    f"is not in the plan. If you need this table, add it "
                    f"to plan.tables explicitly.",
                )
            )
            continue

        if not _has_column(to_tbl, r.to_column):
            if to_tbl.kind == "calculated":
                actions.append(
                    RepairAction(
                        severity="warning",
                        where=rel_where,
                        message=f"Dropping relationship — column '{r.to_column}' "
                        f"is not declared on calculated table '{to_tbl.name}'. "
                        f"Add the column to the plan with a dax_expression.",
                    )
                )
                continue
            added = _add_column_from_schema(to_tbl, r.to_column, rel_where)
            if not added:
                actions.append(
                    RepairAction(
                        severity="warning",
                        where=rel_where,
                        message=f"Dropping relationship — column '{r.to_column}' "
                        f"does not exist in source table '{to_tbl.name}'",
                    )
                )
                continue

        surviving_rels.append(r)

    dropped_count = len(plan.relationships) - len(surviving_rels)
    plan.relationships = surviving_rels
    if dropped_count > 0:
        actions.append(
            RepairAction(
                severity="info",
                where="relationships",
                message=f"Dropped {dropped_count} unrepairable relationship(s) — see warnings above",
            )
        )

    surviving_measures = []
    for i, m in enumerate(plan.measures):
        host = _plan_table(m.host_table)
        if host is None:
            actions.append(
                RepairAction(
                    severity="warning",
                    where=f"measures[{i}] ({m.name})",
                    message=f"Dropping measure — host_table '{m.host_table}' is not in the plan",
                )
            )
            continue
        surviving_measures.append(m)
    plan.measures = surviving_measures

    def _has_renderable_base(t: TableSpec) -> bool:
        key = next((c for c in t.columns if c.is_key), None)
        if key is None:
            return False
        if key.dax_expression:
            return True
        return key.data_type == "dateTime"

    bad_calc_names: set[str] = set()
    surviving_tables = []
    for t in plan.tables:
        if t.kind == "calculated" and not _has_renderable_base(t):
            bad_calc_names.add(t.name)
            actions.append(
                RepairAction(
                    severity="warning",
                    where=f"tables ({t.name})",
                    message=f"Dropping calculated table '{t.name}' — no key column "
                    f"with a dax_expression or dateTime type. The plan needs "
                    f"to either: (a) mark a column is_key=true with a "
                    f"dax_expression like 'CALENDARAUTO()' or "
                    f"'CALENDAR(DATE(2024,1,1), DATE(2027,12,31))', or "
                    f"(b) source this from a warehouse table instead.",
                )
            )
            continue
        surviving_tables.append(t)
    plan.tables = surviving_tables

    if bad_calc_names:
        surviving_rels = []
        for r in plan.relationships:
            if r.from_table in bad_calc_names or r.to_table in bad_calc_names:
                actions.append(
                    RepairAction(
                        severity="warning",
                        where=f"relationships ({r.name})",
                        message=f"Dropping relationship — references a dropped calculated table "
                        f"({bad_calc_names & {r.from_table, r.to_table}})",
                    )
                )
                continue
            surviving_rels.append(r)
        plan.relationships = surviving_rels

        surviving_measures = []
        for m in plan.measures:
            if m.host_table in bad_calc_names:
                actions.append(
                    RepairAction(
                        severity="warning",
                        where=f"measures ({m.name})",
                        message=f"Dropping measure — host_table '{m.host_table}' was dropped",
                    )
                )
                continue
            surviving_measures.append(m)
        plan.measures = surviving_measures

    from collections import deque as _deque

    active_rels = [r for r in plan.relationships if r.is_active]
    rel_graph: dict[str, set[str]] = {}
    for r in active_rels:
        rel_graph.setdefault(r.to_table, set()).add(r.from_table)

    def _reachable(start: str) -> set[str]:
        visited: set[str] = set()
        q = _deque([start])
        while q:
            node = q.popleft()
            if node in visited:
                continue
            visited.add(node)
            q.extend(rel_graph.get(node, ()))
        visited.discard(start)
        return visited

    edges_to_inactivate: set[tuple[str, str]] = set()
    for parent, children in rel_graph.items():
        if len(children) < 2:
            continue
        child_desc: dict[str, set[str]] = {}
        for c in children:
            d = _reachable(c)
            d.add(c)
            child_desc[c] = d
        kids = sorted(children)
        for i in range(len(kids)):
            for j in range(i + 1, len(kids)):
                ci, cj = kids[i], kids[j]
                shared = child_desc[ci] & child_desc[cj]
                if not shared:
                    continue
                if ci in shared:
                    edges_to_inactivate.add((ci, parent))
                elif cj in shared:
                    edges_to_inactivate.add((cj, parent))
                else:
                    edges_to_inactivate.add((cj, parent))

    if edges_to_inactivate:
        for r in plan.relationships:
            if (r.from_table, r.to_table) in edges_to_inactivate and r.is_active:
                r.is_active = False
                actions.append(
                    RepairAction(
                        severity="warning",
                        where=f"relationships ({r.name})",
                        message=(
                            f"Inactivated '{r.from_table} → {r.to_table}' to break "
                            f"an ambiguous filter path. Use USERELATIONSHIP in DAX "
                            f"if you need this edge in a specific measure."
                        ),
                    )
                )

    _remediate_dirty_keys(plan, schema_tables, actions)

    return plan, actions


_M_KEY_CAST: dict[str, str] = {
    "int64": "Int64.Type",
    "double": "type number",
    "decimal": "type number",
    "dateTime": "type datetime",
}


def _remediate_dirty_keys(
    plan: SchemaPlan,
    schema_tables: dict[str, list[tuple[str, str]]],
    actions: list[RepairAction],
) -> None:
    from .validate import _STRING_SQL_TYPES

    one_side = {(r.to_table, r.to_column) for r in plan.relationships}
    for t in plan.tables:
        if t.kind == "calculated":
            continue
        src_types = dict(schema_tables.get(resolve_source_key(t, schema_tables), []))
        for c in t.columns:
            if c.dax_expression or c.m_type_cast or c.data_type == "string":
                continue
            pq = _M_KEY_CAST.get(c.data_type)
            if not pq:
                continue
            if not (c.is_key or (t.name, c.name) in one_side):
                continue
            src_sql = src_types.get(c.source_column, "")
            if src_sql.split("(")[0].strip().lower() not in _STRING_SQL_TYPES:
                continue
            c.m_type_cast = pq
            c.m_row_filter = c.data_type
            actions.append(
                RepairAction(
                    severity="info",
                    where=f"tables ({t.name}).columns ({c.name})",
                    message=(
                        f"Dirty-key remediation: source '{c.source_column}' is "
                        f"{src_sql} (text) but this key is declared {c.data_type}; "
                        f"added an M step to drop non-{c.data_type} rows and cast the "
                        f"rest, so the model loads instead of failing on blank keys."
                    ),
                )
            )


def format_repair_log(actions: list[RepairAction]) -> str:
    if not actions:
        return ""
    lines = []
    for a in actions:
        lines.append(f"  [{a.severity.upper()}] {a.where}: {a.message}")
    return "\n".join(lines)
