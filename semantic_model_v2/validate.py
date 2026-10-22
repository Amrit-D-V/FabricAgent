from __future__ import annotations

import re
from collections import defaultdict, deque

from model.dax_preflight import extract_references

from .plan import PlanError, SchemaPlan, resolve_source_key

_TIME_INTEL_RX = re.compile(
    r"\b(SAMEPERIODLASTYEAR|DATESYTD|DATESMTD|DATESQTD|TOTALYTD|TOTALMTD|TOTALQTD"
    r"|DATEADD|DATESINPERIOD|PARALLELPERIOD|PREVIOUSMONTH|PREVIOUSYEAR|PREVIOUSDAY"
    r"|PREVIOUSQUARTER|NEXTMONTH|NEXTYEAR|NEXTDAY|NEXTQUARTER|ENDOFMONTH|ENDOFYEAR"
    r"|ENDOFQUARTER|STARTOFMONTH|STARTOFYEAR|STARTOFQUARTER|FIRSTDATE|LASTDATE"
    r"|OPENINGBALANCEMONTH|CLOSINGBALANCEMONTH)\s*\(",
    re.IGNORECASE,
)
_DAX_STRIP = re.compile(r'"(?:[^"]|"")*"|//[^\n]*|--[^\n]*|/\*.*?\*/', re.DOTALL)


def _uses_time_intelligence(dax: str) -> bool:
    return bool(_TIME_INTEL_RX.search(_DAX_STRIP.sub(" ", dax or "")))


def _has_marked_date_table(plan: SchemaPlan) -> bool:
    return any(
        t.kind == "calculated"
        and any(c.is_key and c.data_type == "dateTime" for c in t.columns)
        for t in plan.tables
    )


def date_table_gate(plan: SchemaPlan) -> list[str]:
    if _has_marked_date_table(plan):
        return []
    ti_measures = [m.name for m in plan.measures if _uses_time_intelligence(m.dax)]
    if not ti_measures:
        return []
    shown = ", ".join(ti_measures[:6]) + ("…" if len(ti_measures) > 6 else "")
    return [
        (f"{len(ti_measures)} time-intelligence measure(s) but NO marked date table — "
        f"they will return blank/wrong values: {shown}. Add a calculated Date "
        f"dimension (CALENDARAUTO) with a dateTime key and relate it to the fact.")
    ]


_VALID_DATA_TYPES = {"int64", "double", "string", "dateTime", "boolean", "decimal"}

_STRING_SQL_TYPES = {
    "char",
    "nchar",
    "varchar",
    "nvarchar",
    "text",
    "ntext",
    "sysname",
    "uniqueidentifier",
}
UNLOADABLE_TYPE_MARKER = "[unloadable-type]"


def validate_plan(
    plan: SchemaPlan,
    schema_tables: dict[str, list[tuple[str, str]]],
    profile: dict | None = None,
) -> list[PlanError]:
    errors: list[PlanError] = []

    def err(where: str, msg: str) -> None:
        errors.append(PlanError(severity="error", where=where, message=msg))

    def warn(where: str, msg: str) -> None:
        errors.append(PlanError(severity="warning", where=where, message=msg))

    table_names_in_plan: set[str] = set()
    plan_table_cols: dict[str, set[str]] = {}
    plan_table_kinds: dict[str, str] = {}
    one_side = {(r.to_table, r.to_column) for r in plan.relationships}

    for i, t in enumerate(plan.tables):
        where = f"tables[{i}] ({t.name})"

        if t.name in table_names_in_plan:
            err(where, f"Duplicate table in plan: '{t.name}'")
            continue
        table_names_in_plan.add(t.name)
        plan_table_kinds[t.name] = t.kind

        if t.kind == "calculated":
            plan_table_cols[t.name] = {c.name for c in t.columns}
            continue

        src_key = resolve_source_key(t, schema_tables)
        if src_key not in schema_tables:
            err(
                where,
                f"Table '{t.name}' has no source: '{src_key}' is not in the source "
                f"schema. Set `source_table` (+ `schema`) to a real physical table.",
            )
            continue

        schema_cols = {c[0] for c in schema_tables[src_key]}
        src_types = dict(schema_tables[src_key])
        col_names_in_table: set[str] = set()

        for j, c in enumerate(t.columns):
            col_where = f"{where}.columns[{j}] ({c.name})"

            if c.name in col_names_in_table:
                err(col_where, f"Duplicate column '{c.name}' in table '{t.name}'")
                continue
            col_names_in_table.add(c.name)

            if c.data_type not in _VALID_DATA_TYPES:
                err(
                    col_where,
                    f"Invalid data_type '{c.data_type}' (allowed: {sorted(_VALID_DATA_TYPES)})",
                )

            if c.source_column not in schema_cols:
                err(
                    col_where,
                    f"source_column '{c.source_column}' does not exist in source table '{t.name}'. "
                    f"Available columns: {sorted(schema_cols)[:8]}...",
                )

            _src_sql = src_types.get(c.source_column, "")
            if (
                _src_sql
                and _src_sql.split("(")[0].strip().lower() in _STRING_SQL_TYPES
                and c.data_type != "string"
                and not c.m_type_cast
                and not c.dax_expression
            ):
                if c.is_key or (t.name, c.name) in one_side:
                    warn(
                        col_where,
                        f"{UNLOADABLE_TYPE_MARKER} source '{c.source_column}' is "
                        f"{_src_sql} (text) but declared '{c.data_type}', and this is a "
                        f"key/relationship column. Non-{c.data_type} values (blank, "
                        f"footer rows) import as BLANK and blank keys FAIL the load. "
                        f"Set m_type_cast to clean+cast the source, or declare "
                        f"data_type 'string' and match the related column's type.",
                    )
                else:
                    warn(
                        col_where,
                        f"source '{c.source_column}' is {_src_sql} (text) but declared "
                        f"'{c.data_type}'; non-conforming values import as BLANK. "
                        f"Add m_type_cast or declare 'string'.",
                    )

        plan_table_cols[t.name] = col_names_in_table

    _dax_ref_cols = {t.name: {c.name for c in t.columns} for t in plan.tables}
    _dax_ref_measures = {m.name for m in plan.measures}
    for i, t in enumerate(plan.tables):
        for j, c in enumerate(t.columns):
            if not c.dax_expression:
                continue
            col_where = f"tables[{i}] ({t.name}).columns[{j}] ({c.name})"
            ref_cols, ref_bare = extract_references(c.dax_expression)
            for rt, rc in ref_cols:
                known_cols = _dax_ref_cols.get(rt)
                if known_cols is None:
                    err(
                        col_where,
                        f"dax_expression references unknown table '{rt}' - "
                        f"known tables: {sorted(_dax_ref_cols)}",
                    )
                elif rc not in known_cols and rc not in _dax_ref_measures:
                    err(
                        col_where,
                        f"dax_expression references unknown column "
                        f"'{rt}'[{rc}]. Available columns: "
                        f"{sorted(known_cols)[:8]}...",
                    )
            own_cols = _dax_ref_cols.get(t.name, set())
            for ref in ref_bare:
                if ref in own_cols or ref in _dax_ref_measures:
                    continue
                err(
                    col_where,
                    f"dax_expression references '[{ref}]' which is neither "
                    f"a column on '{t.name}' nor a plan measure.",
                )

    _notes_blob = " ".join(plan.notes or []).lower() if hasattr(plan, "notes") else ""
    _is_non_hub_domain = (
        "multi-root-snowflake" in _notes_blob
        or "domain shape: star" in _notes_blob
        or "domain shape: snowflake" in _notes_blob
    )
    fact_tables = [t.name for t in plan.tables if t.kind == "fact"]

    if len(plan.tables) > 1 and not plan.relationships:
        err(
            "relationships",
            f"The plan has {len(plan.tables)} tables but NO relationships. Nothing "
            f"can filter across tables, so every cross-table measure returns the "
            f"wrong answer. Add the fact→dimension joins on the shared keys, or "
            f"move unrelatable tables to excluded_tables with a reason.",
        )
    if len(plan.tables) > 1 and not fact_tables:
        err(
            "tables",
            f"No table is marked kind='fact' — the plan is {len(plan.tables)} "
            f"dimensions with nothing to measure. A star schema needs the "
            f"transaction/event table that the dimensions describe. Include it, "
            f"or mark the correct table as the fact.",
        )

    if fact_tables and not _is_non_hub_domain:
        dim_hit_count: dict[str, int] = defaultdict(int)
        fact_to_dims: dict[str, set[str]] = defaultdict(set)
        for r in plan.relationships:
            if r.from_table in fact_tables:
                fact_to_dims[r.from_table].add(r.to_table)
                dim_hit_count[r.to_table] += 1
        if dim_hit_count:
            hub_table = max(dim_hit_count.items(), key=lambda kv: kv[1])[0]
            hub_count = dim_hit_count[hub_table]
            if hub_count >= 2:
                orphan_facts = [
                    f
                    for f in fact_tables
                    if hub_table not in fact_to_dims.get(f, set())
                ]
                for f in orphan_facts:
                    err(
                        f"tables[{f}]",
                        f"Hub-and-spoke violation: fact '{f}' has no relationship "
                        f"to the hub dim '{hub_table}'. Every fact must join to "
                        f"the hub via its natural key (e.g. policyNumber). Either "
                        f"add the fact→hub relationship or move this fact to "
                        f"excluded_tables with reason 'no hub join'.",
                    )

    # Check for disconnected / orphan dimensions
    if len(plan.tables) > 1 and fact_tables:
        connected_tables: set[str] = set()
        for r in plan.relationships:
            connected_tables.add(r.from_table)
            connected_tables.add(r.to_table)
        for t in plan.tables:
            if t.kind == "dim" and t.name not in connected_tables:
                warn(
                    f"tables[{t.name}]",
                    f"Dimension table '{t.name}' is completely disconnected (0 relationships). "
                    f"Visuals sliced by columns from '{t.name}' will show identical flat values across all categories. "
                    f"Add a relationship connecting '{t.name}' to a fact table or the central hub dimension, "
                    f"or move it to excluded_tables.",
                )

    fact_col_active_targets: dict[tuple[str, str], list[int]] = defaultdict(list)
    fact_dim_pairs_active: dict[tuple[str, str], list[int]] = defaultdict(list)
    rel_graph: dict[str, set[str]] = defaultdict(set)

    for i, r in enumerate(plan.relationships):
        where = f"relationships[{i}] ({r.name})"

        if r.from_table not in plan_table_cols:
            err(where, f"from_table '{r.from_table}' is not in the plan")
            continue
        if r.to_table not in plan_table_cols:
            err(where, f"to_table '{r.to_table}' is not in the plan")
            continue
        if r.from_column not in plan_table_cols[r.from_table]:
            err(
                where,
                f"from_column '{r.from_column}' is not declared on table '{r.from_table}' in the plan",
            )
            continue
        if r.to_column not in plan_table_cols[r.to_table]:
            err(
                where,
                f"to_column '{r.to_column}' is not declared on table '{r.to_table}' in the plan",
            )
            continue

        from_kind = plan_table_kinds.get(r.from_table, "")
        to_kind = plan_table_kinds.get(r.to_table, "")
        if from_kind == "dim" and to_kind == "dim":
            warn(
                where,
                f"Snowflake spoke ({r.from_table} → {r.to_table}). Star is "
                f"preferred when facts carry the spoke's FK directly; this is "
                f"the snowflake fallback and is allowed.",
            )

        if not r.is_active:
            continue

        fact_col_active_targets[(r.from_table, r.from_column)].append(i)
        fact_dim_pairs_active[(r.from_table, r.to_table)].append(i)
        rel_graph[r.to_table].add(r.from_table)

        if profile is not None:
            tinfo = profile.get(r.to_table)
            if tinfo:
                rc = tinfo.get("row_count", 0)
                cinfo = tinfo.get("columns", {}).get(r.to_column)
                if cinfo and rc > 0:
                    distinct = cinfo.get("distinct", 0)
                    nulls = cinfo.get("nulls", 0)
                    if distinct == 1 and rc > 1:
                        err(
                            where,
                            f"'{r.to_table}'.{r.to_column} is CONSTANT in the data "
                            f"(distinct=1 across {rc} rows) — every value is the "
                            f"same, so it cannot uniquely identify a dim row. "
                            f"This will cause uncontrolled fan-out. Pick a "
                            f"different to_column or drop the relationship.",
                        )
                    elif distinct < rc:
                        err(
                            where,
                            f"'{r.to_table}'.{r.to_column} is NOT unique in the data "
                            f"({distinct}/{rc} distinct) — can't be the 'one' side",
                        )
                    if nulls > 0:
                        warn(
                            where,
                            f"'{r.to_table}'.{r.to_column} has {nulls} NULL value(s) "
                            f"— Power BI will reject the relationship on load",
                        )

    for (ft, fc), indices in fact_col_active_targets.items():
        if len(indices) > 1:
            err(
                f"relationships[{indices[0]}+]",
                f"Fact column '{ft}'.{fc} has {len(indices)} active relationships — at most one allowed",
            )

    for (ft, tt), indices in fact_dim_pairs_active.items():
        if len(indices) > 1:
            err(
                f"relationships[{indices[0]}+]",
                f"Same fact-dim pair ({ft} → {tt}) has {len(indices)} active relationships — "
                f"creates ambiguous paths",
            )

    def _reachable(start: str) -> set[str]:
        visited: set[str] = set()
        q = deque([start])
        while q:
            node = q.popleft()
            if node in visited:
                continue
            visited.add(node)
            q.extend(rel_graph.get(node, ()))
        visited.discard(start)
        return visited

    seen_pairs: set[tuple[str, str]] = set()
    for to_table, from_tables in rel_graph.items():
        for ft in from_tables:
            if (ft, to_table) in seen_pairs:
                continue
            seen_pairs.add((ft, to_table))
            other_reach = _reachable(ft) - {to_table}
            for via in other_reach:
                if to_table in rel_graph.get(via, set()):
                    err(
                        f"relationships ({ft} → {to_table})",
                        f"Ambiguous path: {ft} also reaches {to_table} via {via}",
                    )
                    break

    ambiguity_seen: set[tuple[str, str]] = set()
    for source, direct_children in rel_graph.items():
        if len(direct_children) < 2:
            continue
        child_desc: dict[str, set[str]] = {}
        for child in direct_children:
            d = _reachable(child)
            d.add(child)
            child_desc[child] = d
        children_sorted = sorted(direct_children)
        for i in range(len(children_sorted)):
            for j in range(i + 1, len(children_sorted)):
                ci, cj = children_sorted[i], children_sorted[j]
                overlap = child_desc[ci] & child_desc[cj]
                for target in overlap:
                    if (source, target) in ambiguity_seen:
                        continue
                    ambiguity_seen.add((source, target))
                    err(
                        f"relationships ({source} → {target})",
                        f"Ambiguous filter path: {source} → {target} reachable "
                        f"via both {ci} and {cj}. Inactivate one of the edges "
                        f"to break the ambiguity (Power BI rejects the model "
                        f"otherwise).",
                    )

    dim_only_graph: dict[str, set[str]] = defaultdict(set)
    for r in plan.relationships:
        if (
            plan_table_kinds.get(r.from_table) == "dim"
            and plan_table_kinds.get(r.to_table) == "dim"
            and r.is_active
        ):
            dim_only_graph[r.from_table].add(r.to_table)

    def _has_cycle(start: str) -> list[str] | None:
        stack: list[tuple[str, list[str]]] = [(start, [start])]
        visited: set[str] = set()
        while stack:
            node, path = stack.pop()
            if node in visited:
                continue
            visited.add(node)
            for nxt in dim_only_graph.get(node, ()):
                if nxt == start:
                    return path + [nxt]
                if nxt not in visited:
                    stack.append((nxt, path + [nxt]))
        return None

    cycled: set[frozenset[str]] = set()
    for dim_start in list(dim_only_graph.keys()):
        cyc = _has_cycle(dim_start)
        if cyc:
            key = frozenset(cyc)
            if key in cycled:
                continue
            cycled.add(key)
            err(
                "relationships (dim cycle)",
                f"Dim → dim cycle detected: {' → '.join(cyc)}. Snowflake "
                f"spokes must be acyclic. Break one of these edges.",
            )

    measure_names: set[str] = set()
    for i, m in enumerate(plan.measures):
        where = f"measures[{i}] ({m.name})"
        if m.name in measure_names:
            err(
                where,
                f"Duplicate measure name '{m.name}' — must be unique across the model",
            )
        measure_names.add(m.name)
        if m.host_table not in plan_table_cols:
            err(where, f"host_table '{m.host_table}' is not in the plan")
        if not m.intent.strip():
            warn(where, "Empty intent — Phase 6 will have trouble writing good DAX")

    for t_name, cols in plan_table_cols.items():
        col_lower_to_orig = {c.lower(): c for c in cols}
        for m in plan.measures:
            if m.host_table != t_name:
                continue
            collide = col_lower_to_orig.get(m.name.lower())
            if collide is not None:
                err(
                    f"measures (host={t_name})",
                    f"Measure '{m.name}' collides with column '{collide}' on "
                    f"'{t_name}' (Power BI's name check is case-insensitive). "
                    f"Rename the measure — e.g. 'Total {m.name}' or "
                    f"'Sum of {m.name}'.",
                )

    seen_measures_ci: dict[str, str] = {}
    for m in plan.measures:
        prior = seen_measures_ci.get(m.name.lower())
        if prior is not None and prior != m.name:
            err(
                f"measures ({m.name})",
                f"Measure '{m.name}' duplicates earlier '{prior}' "
                f"(case-insensitive). Rename one of them.",
            )
        else:
            seen_measures_ci[m.name.lower()] = m.name

    return errors


def format_errors(errors: list[PlanError]) -> str:
    if not errors:
        return "(no errors)"
    lines = []
    for e in errors:
        lines.append(f"  [{e.severity.upper()}] {e.where}: {e.message}")
    return "\n".join(lines)
