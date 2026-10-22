from __future__ import annotations

from config import (
    SMB_PROFILE_MAX_TABLES,
    SMB_PROFILE_SAMPLE_CAP,
    SMB_PROFILE_SKIP_SCHEMAS,
)
from fabric.warehouse_schema import (
    _connect_aad_password,
    _connect_service_principal,
    _connect_sql_auth,
    _connect_token,
    _open_conn,
    _parse_schema_ddl,
)
from util.logger_setup import get_logger
from util.sql_safety import is_read_only_sql

logger = get_logger(__name__)


_PROFILE_SKIP_SCHEMAS = frozenset(SMB_PROFILE_SKIP_SCHEMAS)


PROFILE_MAX_TABLES = SMB_PROFILE_MAX_TABLES


def _profile_cap_message(profile: dict, schema_text: str) -> str:
    eligible = [
        t
        for t in _parse_schema_ddl(schema_text)
        if (t.split(".", 1)[0] if "." in t else "") not in _PROFILE_SKIP_SCHEMAS
    ]
    profiled = len(profile or {})
    if profiled and len(eligible) > profiled:
        return (
            f"Profiled {profiled} of {len(eligible)} tables (capped at "
            f"{PROFILE_MAX_TABLES}). Relationships on the unprofiled tables are "
            "designed without uniqueness checks — review them carefully."
        )
    return ""


def _profile_data(
    server: str,
    db_name: str,
    auth_mode: str,
    schema_text: str,
    username: str = "",
    password: str = "",
    sample_rows: int = 20,
    max_tables: int = PROFILE_MAX_TABLES,
) -> dict:
    tables = _parse_schema_ddl(schema_text)
    if not tables:
        return {}

    try:
        if auth_mode == "Service Principal (connection string)":
            conn = _connect_service_principal(server, db_name)
        elif auth_mode == "SQL Username + Password":
            conn = _connect_sql_auth(server, db_name, username, password)
        elif auth_mode == "AAD Username + Password (your Fabric login)":
            conn = _connect_aad_password(server, db_name, username, password)
        else:
            conn = _connect_token(server, db_name)
    except Exception as e:
        logger.warning(f"Profile fetch: could not connect ({e})")
        return {}

    cursor = conn.cursor()
    profile: dict = {}

    def _quote_tbl(full: str) -> str:
        if "." in full:
            sch, name = full.split(".", 1)
            return f"[{sch}].[{name}]"
        return f"[{full}]"

    for i, (full_name, cols) in enumerate(tables.items()):
        if i >= max_tables:
            break
        schema_part = full_name.split(".", 1)[0] if "." in full_name else ""
        if schema_part in _PROFILE_SKIP_SCHEMAS:
            continue
        qtbl = _quote_tbl(full_name)
        col_names = [c[0] for c in cols]
        tinfo = {"row_count": 0, "columns": {}, "samples": []}

        try:
            cursor.execute(f"SELECT COUNT(*) FROM {qtbl}")
            cnt_row = cursor.fetchone()
            tinfo["row_count"] = (cnt_row[0] if cnt_row else 0) or 0
        except Exception as e:
            logger.warning(f"Profile: COUNT failed for {qtbl}: {e}")
            profile[full_name] = tinfo
            continue

        if tinfo["row_count"] == 0:
            profile[full_name] = tinfo
            continue

        chunk_size = 25
        for start in range(0, len(col_names), chunk_size):
            chunk = col_names[start : start + chunk_size]
            select_parts: list[str] = []
            for c in chunk:
                select_parts.append(
                    f"COUNT(DISTINCT [{c}]) AS d_{start}_{chunk.index(c)}"
                )
                select_parts.append(
                    f"SUM(CASE WHEN [{c}] IS NULL THEN 1 ELSE 0 END) AS n_{start}_{chunk.index(c)}"
                )
            try:
                cursor.execute(f"SELECT {', '.join(select_parts)} FROM {qtbl}")
                row = cursor.fetchone()
                if row is not None:
                    for j, c in enumerate(chunk):
                        distinct = row[j * 2] or 0
                        nulls = row[j * 2 + 1] or 0
                        tinfo["columns"][c] = {
                            "distinct": int(distinct),
                            "nulls": int(nulls),
                            "samples": [],
                        }
            except Exception as e:
                logger.warning(
                    f"Profile: cardinality query failed for {qtbl} cols {chunk}: {e}"
                )

        try:
            cursor.execute(f"SELECT TOP {sample_rows} * FROM {qtbl}")
            cols_in_order = [d[0] for d in cursor.description or []]
            for r in cursor.fetchall():
                row_dict = {c: r[i] for i, c in enumerate(cols_in_order)}
                tinfo["samples"].append(row_dict)
        except Exception as e:
            logger.warning(f"Profile: sample fetch failed for {qtbl}: {e}")

        for _c, _cinfo in tinfo["columns"].items():
            _vals = [r.get(_c) for r in tinfo["samples"] if r.get(_c) is not None]
            _cinfo["samples"] = _vals[:SMB_PROFILE_SAMPLE_CAP]

        profile[full_name] = tinfo

    conn.close()
    return profile


def _distinct_member_block(
    server: str,
    db_name: str,
    auth_mode: str,
    profile: dict,
    username: str = "",
    password: str = "",
    member_cap: int = 50,
    max_cols: int = 30,
) -> str:
    targets: list[tuple[str, str, str]] = []
    for full_name, info in profile.items():
        if "." in full_name:
            sch, tbl = full_name.split(".", 1)
            qtbl = f"[{sch}].[{tbl}]"
        else:
            qtbl = f"[{full_name}]"
        for col, stats in info.get("columns", {}).items():
            d = stats.get("distinct", 0)
            if 1 < d <= member_cap:
                targets.append((full_name, qtbl, col))
    targets = targets[:max_cols]
    if not targets:
        return ""
    try:
        conn = _open_conn(server, db_name, auth_mode, username, password)
    except Exception as e:
        logger.warning(
            f"distinct-member block: connection failed ({e}); profile partial"
        )
        return (
            "## Full categorical members UNAVAILABLE (could not connect for SELECT "
            "DISTINCT) — treat the profile as partial; do NOT invent filter literals."
        )
    blocks: list[str] = []
    try:
        cur = conn.cursor()
        for full_name, qtbl, col in targets:
            sql = f"SELECT DISTINCT [{col}] AS v FROM {qtbl}"
            ok, _ = is_read_only_sql(sql)
            if not ok:
                continue
            try:
                cur.execute(sql)
                vals = [
                    str(r[0]) for r in cur.fetchmany(member_cap) if r[0] is not None
                ]
            except Exception as e:
                logger.warning(
                    f"distinct-member: query failed for {full_name}.{col} ({e})"
                )
                continue
            if vals:
                blocks.append(f"- {full_name}.{col} ({len(vals)}): " + ", ".join(vals))
    finally:
        conn.close()
    if not blocks:
        return ""
    return (
        "## Full categorical members (real values from the warehouse — use these verbatim)\n"
        + "\n".join(blocks)
    )


def _profile_to_context(
    profile: dict, schema_text: str = "", max_distinct_samples: int = 15
) -> str:
    if not profile:
        return ""
    lines: list[str] = []
    _cap = _profile_cap_message(profile, schema_text) if schema_text else ""
    if _cap:
        lines.append("⚠ PROFILE INCOMPLETE: " + _cap)
    lines += [
        "DATA PROFILE (real numbers from the warehouse — use these to pick PK candidates and infer measures):",
        "(format: <table> [<row_count> rows]\\n  <col>: <distinct> distinct, <nulls> nulls, sample=[...])",
    ]
    for full_name, info in profile.items():
        rc = info.get("row_count", 0)
        lines.append(f"\n  {full_name} [{rc:,} rows]:")
        cols = info.get("columns", {})
        samples = info.get("samples", [])
        sample_vals_per_col: dict[str, list] = {}
        for s in samples:
            for c, v in s.items():
                sample_vals_per_col.setdefault(c, [])
                if (
                    v is not None
                    and v not in sample_vals_per_col[c]
                    and len(sample_vals_per_col[c]) < max_distinct_samples
                ):
                    sample_vals_per_col[c].append(v)
        for c, stats in cols.items():
            distinct = stats.get("distinct", 0)
            nulls = stats.get("nulls", 0)
            tag_parts = []
            if rc > 0:
                if distinct == rc:
                    tag_parts.append("UNIQUE")
                elif distinct == 1:
                    tag_parts.append("CONSTANT")
                if nulls == rc:
                    tag_parts.append("ALL_NULL")
                elif nulls / rc > 0.5:
                    tag_parts.append("MOSTLY_NULL")
            tag = (" [" + ", ".join(tag_parts) + "]") if tag_parts else ""
            sample_strs = [
                str(v)[:30] + ("…" if len(str(v)) > 30 else "")
                for v in sample_vals_per_col.get(c, [])
            ]
            sample_part = f" sample={sample_strs}" if sample_strs else ""
            lines.append(
                f"    {c}: {distinct} distinct, {nulls} nulls{tag}{sample_part}"
            )
    return "\n".join(lines)
