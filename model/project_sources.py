from __future__ import annotations

from dataclasses import dataclass, field

from util.logger_setup import get_logger
from util.sql_safety import is_read_only_sql

logger = get_logger(__name__)

_SAMPLE_TABLES = 12
_SAMPLE_VALUES = 8
_PROFILE_MAX_CHARS = 12000


@dataclass
class SourceProbe:
    ok: bool = False
    error: str = ""
    summary: str = ""
    block: str = ""
    tables: list[dict] = field(default_factory=list)
    # Set when the probe canonicalises what it was handed (a pasted portal URL
    # rewritten to the powerbi:// endpoint), so the stored config keeps the
    # address that actually connected rather than the one that was typed.
    server: str = ""


def _grouped_tables(schema) -> list[dict]:
    by_table: dict[str, list[str]] = {}
    for _schema, table, col, _type in schema.columns:
        by_table.setdefault(f"{_schema}.{table}", []).append(col)
    return [{"name": name, "columns": cols} for name, cols in by_table.items()]


def probe_sql_source(
    server: str,
    database: str,
    auth_mode: str,
    username: str = "",
    password: str = "",
    profile: bool = True,
) -> SourceProbe:
    from fabric.fabric_sql import fetch_schema

    schema = fetch_schema(server, database, auth_mode, username, password)
    if schema.error:
        return SourceProbe(error=schema.error)

    out = SourceProbe(
        ok=True,
        summary=schema.summary(),
        block=schema.to_prompt_block(max_chars=20000),
        tables=_grouped_tables(schema),
    )
    if profile:
        sampled = _sample_values(
            server, database, auth_mode, username, password, out.tables
        )
        if sampled:
            out.block += "\n\n" + sampled
    return out


def _sample_values(
    server: str,
    database: str,
    auth_mode: str,
    username: str,
    password: str,
    tables: list[dict],
) -> str:
    from fabric.fabric_sql import _open_connection

    try:
        conn = _open_connection(server, database, auth_mode, username, password)
    except Exception:
        return ""

    lines = ["## Sample values (read-only, TOP rows)"]
    try:
        cursor = conn.cursor()
        for t in tables[:_SAMPLE_TABLES]:
            schema_name, _, table_name = t["name"].partition(".")
            for col in t["columns"][:4]:
                sql = (
                    f"SELECT DISTINCT TOP {_SAMPLE_VALUES} [{col}] "
                    f"FROM [{schema_name}].[{table_name}] "
                    f"WHERE [{col}] IS NOT NULL"
                )
                ok, why = is_read_only_sql(sql)
                if not ok:
                    lines.append(f"  - {t['name']}.{col}: skipped ({why})")
                    continue
                try:
                    cursor.execute(sql)
                    vals = [str(r[0])[:40] for r in cursor.fetchall()]
                except Exception as e:
                    logger.debug("sample query failed for %s.%s: %s", t["name"], col, e)
                    continue
                if vals:
                    lines.append(f"  - {t['name']}.{col}: " + ", ".join(vals))
            if len("\n".join(lines)) > _PROFILE_MAX_CHARS:
                lines.append("  … sampling stopped at the profile budget.")
                break
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return "\n".join(lines) if len(lines) > 1 else ""


def probe_semantic_model(server: str, database: str) -> SourceProbe:
    from fabric.pbi_xmla import (
        fetch_model_metadata,
        normalize_xmla_endpoint,
        relationship_label,
    )

    try:
        server = normalize_xmla_endpoint(server)
        meta = fetch_model_metadata(server, database)
    except Exception as e:
        # The cap is generous because these messages carry the fix, not just the
        # symptom — truncating them mid-instruction helps nobody.
        return SourceProbe(error=f"XMLA connect failed: {str(e)[:800]}")

    table_names = meta.get("tables") or []
    columns_by_table = meta.get("columns_by_table") or {}
    measures_by_table = meta.get("measures_by_table") or {}
    relationships = meta.get("relationships") or []
    row_counts = meta.get("row_counts") or {}

    lines = [f"## Existing semantic model — {database}", f"_Server:_ `{server}`", ""]
    for table in table_names:
        rows = row_counts.get(table)
        header = f"**{table}**" + (
            f"  ({rows:,} rows)" if isinstance(rows, int) else ""
        )
        lines.append(header)
        for col, dtype in columns_by_table.get(table, []):
            lines.append(f"  - `{col}`  *{dtype}*")
        for m in measures_by_table.get(table, []):
            lines.append(f"  - _measure_ `{m}`")
    if relationships:
        lines.append("\n**Relationships**")
        for r in relationships:
            flag = "" if r.get("is_active") else "  (inactive)"
            lines.append(f"  - {relationship_label(r)}{flag}")
    for err in meta.get("measure_errors") or []:
        lines.append(f"  ! broken measure — {err}")

    total_measures = sum(len(v) for v in measures_by_table.values())
    return SourceProbe(
        ok=True,
        summary=f"{len(table_names)} tables, {total_measures} measures",
        block="\n".join(lines),
        server=server,
        tables=[
            {"name": t, "columns": [c for c, _ in columns_by_table.get(t, [])]}
            for t in table_names
        ],
    )
