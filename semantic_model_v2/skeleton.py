from __future__ import annotations

import uuid

from .plan import (
    ColumnSpec,
    MeasureSpec,
    RelationshipSpec,
    SchemaPlan,
    TableSpec,
)


def _lineage_tag() -> str:
    return str(uuid.uuid4())


def _quoted(name: str) -> str:
    if "." in name or " " in name:
        return f"'{name}'"
    return name


def _build_calculated_table_source(t: TableSpec) -> str:
    key_col = next((c for c in t.columns if c.is_key), None)
    if key_col and key_col.dax_expression:
        base = key_col.dax_expression
    elif key_col and key_col.data_type == "dateTime":
        base = "CALENDARAUTO()"
    else:
        return "{}"

    extras: list[str] = []
    for c in t.columns:
        if c is key_col:
            continue
        if c.dax_expression:
            extras.append(f'"{c.name}", {c.dax_expression}')
    if not extras:
        return base
    return f"ADDCOLUMNS({base}, " + ", ".join(extras) + ")"


def _emit_is_key(
    c: ColumnSpec,
    *,
    table_name: str,
    table_kind: str,
    profile: dict | None,
) -> bool:
    if not c.is_key:
        return False
    if table_kind == "fact":
        return False
    if profile:
        tinfo = profile.get(table_name) or {}
        cols = tinfo.get("columns") or {}
        cinfo = cols.get(c.source_column) or cols.get(c.name)
        if cinfo is not None:
            nulls = cinfo.get("nulls")
            if nulls is not None and nulls > 0:
                return False
            distinct = cinfo.get("distinct")
            row_count = tinfo.get("row_count")
            if distinct is not None and row_count is not None and distinct != row_count:
                return False
    return True


def render_column(
    c: ColumnSpec,
    *,
    table_name: str,
    is_calculated_table: bool = False,
    table_kind: str = "dim",
    profile: dict | None = None,
) -> str:
    if not is_calculated_table and c.dax_expression:
        lines = [f"\tcolumn {_quoted(c.name)} = {c.dax_expression.strip()}"]
        lines.append(f"\t\tdataType: {c.data_type}")
    else:
        raw_src = c.source_column
        if is_calculated_table:
            if not raw_src or raw_src in ("None", "null"):
                src = f"[{c.name}]"
            elif raw_src.startswith("[") and raw_src.endswith("]"):
                src = raw_src
            else:
                src = f"[{raw_src}]"
        else:
            src = raw_src or c.name
        lines = [f"\tcolumn {_quoted(c.name)}"]
        lines.append(f"\t\tdataType: {c.data_type}")
        lines.append(f"\t\tsourceColumn: {src}")
    lines.append(f"\t\tlineageTag: {_lineage_tag()}")
    if c.summarize_by:
        lines.append(f"\t\tsummarizeBy: {c.summarize_by}")
    if c.is_hidden:
        lines.append("\t\tisHidden: true")
    if _emit_is_key(c, table_name=table_name, table_kind=table_kind, profile=profile):
        lines.append("\t\tisKey: true")
    if c.display_folder:
        lines.append(f"\t\tdisplayFolder: {c.display_folder}")
    if c.sort_by:
        lines.append(f"\t\tsortByColumn: {_quoted(c.sort_by)}")
    if c.synonyms:
        joined = "|".join(s.replace('"', '\\"') for s in c.synonyms)
        lines.append(f'\t\tannotation Synonyms = "{joined}"')
    return "\n".join(lines)


def render_measure(m: MeasureSpec) -> str:
    if not m.dax.strip():
        raise ValueError(
            f"Measure '{m.name}' has empty DAX — Phase 6 must run before Phase 5"
        )
    head = f"\tmeasure {_quoted(m.name)} = {m.dax.strip()}"
    lines = [head]
    lines.append(f"\t\tlineageTag: {_lineage_tag()}")
    if m.format_string:
        lines.append(f"\t\tformatString: {m.format_string}")
    if m.display_folder:
        lines.append(f"\t\tdisplayFolder: {m.display_folder}")
    if m.synonyms:
        joined = "|".join(s.replace('"', '\\"') for s in m.synonyms)
        lines.append(f'\t\tannotation Synonyms = "{joined}"')
    return "\n".join(lines)


_M_VALUE_COERCE: dict[str, tuple[str, str]] = {
    "int64": ("Int64.From", "Int64.Type"),
    "dateTime": ("DateTime.From", "type datetime"),
}

_M_ROW_FILTER: dict[str, tuple[str, str]] = {
    "int64": ("Number.From", "number"),
    "double": ("Number.From", "number"),
    "decimal": ("Number.From", "number"),
    "dateTime": ("DateTime.From", "datetime"),
}


def render_partition_m(
    table_name: str,
    schema: str,
    source_table: str,
    server_fqdn: str,
    db_name: str,
    column_names: list[str],
    casts: list[tuple[str, str]] | None = None,
    coerces: list[tuple[str, str]] | None = None,
    filters: list[tuple[str, str]] | None = None,
) -> str:
    col_proj = ", ".join(f'"{c}"' for c in column_names)
    src_db = db_name.replace('"', '""')
    steps = [
        f'\t\t\t    Source = Sql.Database("{server_fqdn}", "{src_db}")',
        f'\t\t\t    Data = Source{{[Schema="{schema}",Item="{source_table}"]}}[Data]',
        f"\t\t\t    Projected = Table.SelectColumns(Data, {{{col_proj}}})",
    ]
    final = "Projected"
    if filters:
        preds = " and ".join(
            f"(try {_M_ROW_FILTER.get(tok, ('Number.From', 'number'))[0]}([{src}]) "
            f"is {_M_ROW_FILTER.get(tok, ('Number.From', 'number'))[1]} otherwise false)"
            for src, tok in filters
        )
        steps.append(f"\t\t\t    Filtered = Table.SelectRows({final}, each {preds})")
        final = "Filtered"
    if casts:
        transforms = ", ".join(f'{{"{src}", {pq}}}' for src, pq in casts)
        steps.append(
            f"\t\t\t    Typed = Table.TransformColumnTypes({final}, {{{transforms}}})"
        )
        final = "Typed"
    if coerces:
        parts = []
        for src, tok in coerces:
            fn, ty = _M_VALUE_COERCE.get(tok, ("Value.From", "type any"))
            parts.append(f'{{"{src}", each try {fn}(_) otherwise null, {ty}}}')
        steps.append(
            f"\t\t\t    Coerced = Table.TransformColumns({final}, {{{', '.join(parts)}}})"
        )
        final = "Coerced"
    body = ",\n".join(steps)
    return (
        f"\tpartition {_quoted(table_name)} = m\n"
        f"\t\tmode: import\n"
        f"\t\tsource = ```\n"
        f"\t\t\tlet\n"
        f"{body}\n"
        f"\t\t\tin\n"
        f"\t\t\t    {final}\n"
        f"\t\t\t```"
    )


def render_partition_calculated(table_name: str, dax_expression: str) -> str:
    return (
        f"\tpartition {_quoted(table_name)} = calculated\n"
        f"\t\tmode: import\n"
        f"\t\tsource = {dax_expression}"
    )


def render_table(
    t: TableSpec,
    measures: list[MeasureSpec],
    server_fqdn: str,
    db_name: str,
    profile: dict | None = None,
) -> str:
    lines: list[str] = []
    head = f"table {_quoted(t.name)}"
    lines.append(head)
    lines.append(f"\tlineageTag: {_lineage_tag()}")
    if t.kind == "calculated" and any(
        c.is_key and c.data_type == "dateTime" for c in t.columns
    ):
        lines.append("\tdataCategory: Time")
    if t.is_hidden:
        lines.append("\tisHidden: true")
    if t.synonyms:
        joined = "|".join(s.replace('"', '\\"') for s in t.synonyms)
        lines.append(f'\tannotation Synonyms = "{joined}"')
    lines.append("")

    host_measures = [m for m in measures if m.host_table == t.name]
    for m in host_measures:
        lines.append(render_measure(m))
        lines.append("")

    is_calc = t.kind == "calculated"

    for c in t.columns:
        lines.append(
            render_column(
                c,
                table_name=t.name,
                is_calculated_table=is_calc,
                table_kind=t.kind,
                profile=profile,
            )
        )
        lines.append("")

    if is_calc:
        lines.append(
            render_partition_calculated(t.name, _build_calculated_table_source(t))
        )
    else:
        if not t.schema or not t.source_table:
            raise ValueError(
                f"Table '{t.name}' is kind='{t.kind}' but missing schema/source_table — "
                f"Phase 4 should have caught this"
            )
        col_names = [c.source_column for c in t.columns if not c.dax_expression]
        casts = [
            (c.source_column, c.m_type_cast)
            for c in t.columns
            if c.m_type_cast and not c.dax_expression
        ]
        coerces = [
            (c.source_column, c.m_value_coerce)
            for c in t.columns
            if c.m_value_coerce and not c.dax_expression
        ]
        filters = [
            (c.source_column, c.m_row_filter)
            for c in t.columns
            if c.m_row_filter and not c.dax_expression
        ]
        lines.append(
            render_partition_m(
                t.name,
                t.schema,
                t.source_table,
                server_fqdn,
                db_name,
                col_names,
                casts=casts,
                coerces=coerces,
                filters=filters,
            )
        )

    return "\n".join(lines) + "\n"


def render_relationship(r: RelationshipSpec) -> str:
    lines = [f"relationship {_quoted(r.name)}"]
    if not r.is_active:
        lines.append("\tisActive: false")
    if r.cross_filter == "bothDirections":
        lines.append("\tcrossFilteringBehavior: bothDirections")
    lines.append(f"\tfromColumn: {_quoted(r.from_table)}.{_quoted(r.from_column)}")
    lines.append(f"\ttoColumn: {_quoted(r.to_table)}.{_quoted(r.to_column)}")
    return "\n".join(lines)


def render_relationships(plan: SchemaPlan) -> str:
    if not plan.relationships:
        return ""
    return "\n\n".join(render_relationship(r) for r in plan.relationships) + "\n"


def render_model(plan: SchemaPlan) -> str:
    lines = [
        "model Model",
        "\tculture: en-US",
        "\tdefaultPowerBIDataSourceVersion: powerBI_V3",
        "",
        'annotation PBI_ProTooling = ["DevMode"]',
        "",
    ]
    for t in plan.tables:
        lines.append(f"ref table {_quoted(t.name)}")
    return "\n".join(lines) + "\n"


def build_skeleton(
    plan: SchemaPlan, progress_callback=None, profile: dict | None = None
) -> dict[str, str]:
    files: dict[str, str] = {}
    if progress_callback:
        progress_callback("Phase 7: rendering model.tmdl + relationships.tmdl...")
    files["model.tmdl"] = render_model(plan)
    rel_content = render_relationships(plan)
    if rel_content:
        files["relationships.tmdl"] = rel_content
    n = len(plan.tables)
    for i, t in enumerate(plan.tables, 1):
        if progress_callback:
            n_cols = len(t.columns)
            n_meas = sum(1 for m in plan.measures if m.host_table == t.name)
            progress_callback(
                f"Phase 7: rendering table {i}/{n} — {t.name} "
                f"({n_cols} cols · {n_meas} measures)"
            )
        files[f"tables/{t.name}.tmdl"] = render_table(
            t, plan.measures, plan.server_fqdn, plan.db_name, profile=profile
        )
    return files
