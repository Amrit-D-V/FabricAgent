from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class ColumnSpec:

    name: str
    source_column: str
    data_type: str
    is_hidden: bool = False
    is_key: bool = False
    summarize_by: str | None = None
    display_folder: str | None = None
    description: str | None = None
    dax_expression: str | None = None
    synonyms: list[str] | None = None
    sort_by: str | None = None
    m_type_cast: str | None = None
    m_value_coerce: str | None = None
    m_row_filter: str | None = None


@dataclass
class MeasureSpec:

    name: str
    host_table: str
    intent: str
    template_id: str | None = None
    format_string: str | None = None
    display_folder: str | None = None
    dax: str = ""
    synonyms: list[str] | None = None
    auto_injected: bool = False


@dataclass
class RelationshipSpec:

    from_table: str
    from_column: str
    to_table: str
    to_column: str
    is_active: bool = True
    cross_filter: Literal["singleDirection", "bothDirections"] = "singleDirection"
    rationale: str = ""

    @property
    def name(self) -> str:
        return (
            f"{self.from_table}_{self.from_column}_{self.to_table}_{self.to_column}".replace(
                ".", "_"
            )
            .replace("'", "")
            .replace(" ", "_")
        )


@dataclass
class TableSpec:

    name: str
    kind: Literal["fact", "dim", "calculated"]
    schema: str | None = None
    source_table: str | None = None
    columns: list[ColumnSpec] = field(default_factory=list)
    is_hidden: bool = False
    description: str | None = None
    synonyms: list[str] | None = None


@dataclass
class SchemaPlan:

    model_name: str
    server_fqdn: str
    db_name: str
    tables: list[TableSpec] = field(default_factory=list)
    relationships: list[RelationshipSpec] = field(default_factory=list)
    measures: list[MeasureSpec] = field(default_factory=list)
    excluded_tables: list[tuple[str, str]] = field(default_factory=list)
    excluded_relationships: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class PlanError:

    severity: Literal["error", "warning"]
    where: str
    message: str


def resolve_source_key(
    t: TableSpec, schema_tables: dict[str, list[tuple[str, str]]]
) -> str:
    candidates: list[str] = []
    if t.schema and t.source_table:
        candidates.append(f"{t.schema}.{t.source_table}")
    if t.source_table:
        candidates.append(t.source_table)
    candidates.append(t.name)
    for c in candidates:
        if c in schema_tables:
            return c
    return candidates[0]


def plan_to_dict(plan: SchemaPlan) -> dict:
    return {
        "model_name": plan.model_name,
        "server_fqdn": plan.server_fqdn,
        "db_name": plan.db_name,
        "tables": [
            {
                "name": t.name,
                "kind": t.kind,
                "schema": t.schema,
                "source_table": t.source_table,
                "is_hidden": t.is_hidden,
                "description": t.description,
                "synonyms": t.synonyms,
                "columns": [
                    {
                        "name": c.name,
                        "source_column": c.source_column,
                        "data_type": c.data_type,
                        "is_hidden": c.is_hidden,
                        "is_key": c.is_key,
                        "summarize_by": c.summarize_by,
                        "display_folder": c.display_folder,
                        "description": c.description,
                        "dax_expression": c.dax_expression,
                        "synonyms": c.synonyms,
                        "sort_by": c.sort_by,
                        "m_type_cast": c.m_type_cast,
                        "m_value_coerce": c.m_value_coerce,
                        "m_row_filter": c.m_row_filter,
                    }
                    for c in t.columns
                ],
            }
            for t in plan.tables
        ],
        "relationships": [
            {
                "from_table": r.from_table,
                "from_column": r.from_column,
                "to_table": r.to_table,
                "to_column": r.to_column,
                "is_active": r.is_active,
                "cross_filter": r.cross_filter,
                "rationale": r.rationale,
            }
            for r in plan.relationships
        ],
        "measures": [
            {
                "name": m.name,
                "host_table": m.host_table,
                "intent": m.intent,
                "template_id": m.template_id,
                "format_string": m.format_string,
                "display_folder": m.display_folder,
                "dax": m.dax,
                "synonyms": m.synonyms,
                "auto_injected": m.auto_injected,
            }
            for m in plan.measures
        ],
        "excluded_tables": [list(x) for x in plan.excluded_tables],
        "excluded_relationships": [list(x) for x in plan.excluded_relationships],
        "notes": plan.notes,
    }


def dict_to_plan(data: dict) -> SchemaPlan:
    return SchemaPlan(
        model_name=data["model_name"],
        server_fqdn=data.get("server_fqdn", ""),
        db_name=data.get("db_name", ""),
        tables=[
            TableSpec(
                name=t["name"],
                kind=t.get("kind", "dim"),
                schema=t.get("schema"),
                source_table=t.get("source_table"),
                is_hidden=t.get("is_hidden", False),
                description=t.get("description"),
                synonyms=t.get("synonyms"),
                columns=[
                    ColumnSpec(
                        name=c["name"],
                        source_column=c.get("source_column", c["name"]),
                        data_type=c.get("data_type", "string"),
                        is_hidden=c.get("is_hidden", False),
                        is_key=c.get("is_key", False),
                        summarize_by=c.get("summarize_by"),
                        display_folder=c.get("display_folder"),
                        description=c.get("description"),
                        dax_expression=c.get("dax_expression"),
                        synonyms=c.get("synonyms"),
                        sort_by=c.get("sort_by"),
                        m_type_cast=c.get("m_type_cast"),
                        m_value_coerce=c.get("m_value_coerce"),
                        m_row_filter=c.get("m_row_filter"),
                    )
                    for c in t.get("columns", [])
                ],
            )
            for t in data.get("tables", [])
        ],
        relationships=[
            RelationshipSpec(
                from_table=r["from_table"],
                from_column=r["from_column"],
                to_table=r["to_table"],
                to_column=r["to_column"],
                is_active=r.get("is_active", True),
                cross_filter=r.get("cross_filter", "singleDirection"),
                rationale=r.get("rationale", ""),
            )
            for r in data.get("relationships", [])
        ],
        measures=[
            MeasureSpec(
                name=m["name"],
                host_table=m["host_table"],
                intent=m.get("intent", ""),
                template_id=m.get("template_id"),
                format_string=m.get("format_string"),
                display_folder=m.get("display_folder"),
                dax=m.get("dax", ""),
                synonyms=m.get("synonyms"),
                auto_injected=m.get("auto_injected", False),
            )
            for m in data.get("measures", [])
        ],
        excluded_tables=[tuple(x) for x in data.get("excluded_tables", [])],
        excluded_relationships=[
            tuple(x) for x in data.get("excluded_relationships", [])
        ],
        notes=data.get("notes", []),
    )
