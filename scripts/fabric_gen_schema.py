"""Generate T-SQL DDL from the live SQLite schema.

Hand-translating 404 lines invites silent drift, so the schema is read from
the database that is actually in use and converted column by column.
"""

import sqlite3
import sys

# TEXT columns that are a PK or UNIQUE cannot be NVARCHAR(MAX) in T-SQL --
# indexed columns need a bounded length.
SIZED = 450


def tsql_type(decl: str, pk: bool, unique: bool) -> str:
    d = (decl or "TEXT").upper()
    if "INT" in d:
        return "BIGINT" if "BIGINT" in d else "INT"
    if any(k in d for k in ("CHAR", "CLOB", "TEXT")):
        return f"NVARCHAR({SIZED})" if (pk or unique) else "NVARCHAR(MAX)"
    if "BLOB" in d:
        return "VARBINARY(MAX)"
    if any(k in d for k in ("REAL", "FLOA", "DOUB")):
        return "FLOAT"
    if "BOOL" in d or d == "BIT":
        return "BIT"
    if "TIMESTAMP" in d or "DATETIME" in d:
        return "NVARCHAR(64)"
    if "NUMERIC" in d or "DECIMAL" in d:
        return "DECIMAL(18,4)"
    return "NVARCHAR(MAX)"


def convert(db: str) -> str:
    c = sqlite3.connect(db)
    out = []
    tables = [
        r[0]
        for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    for t in tables:
        info = list(c.execute(f'PRAGMA table_info("{t}")'))
        sql = (
            c.execute("SELECT sql FROM sqlite_master WHERE name=?", (t,)).fetchone()[0]
            or ""
        )
        autoinc = "AUTOINCREMENT" in sql.upper()
        # Keep each unique index as a TUPLE. Flattening them into a set of
        # column names turns a composite constraint into several single-column
        # ones, which is far stricter: projects is unique on
        # (client_id, name), and UNIQUE(client_id) alone rejects a client's
        # second project.
        unique_groups = []
        for idx in c.execute(f'PRAGMA index_list("{t}")'):
            if idx[2]:  # unique
                cols_in_idx = [
                    col[2] for col in c.execute(f'PRAGMA index_info("{idx[1]}")')
                ]
                unique_groups.append(cols_in_idx)
        # EVERY column in a unique index needs a bounded NVARCHAR length, not
        # just single-column ones: SQL Server refuses NVARCHAR(MAX) as an index
        # key, so a composite unique over a MAX column fails at CREATE TABLE.
        uniques = {col for g in unique_groups for col in g}
        pk_cols = [r[1] for r in info if r[5]]
        lines = []
        for cid, name, decl, notnull, dflt, pk in info:
            typ = tsql_type(decl, bool(pk), name in uniques)
            if pk and autoinc and "INT" in typ:
                typ += " IDENTITY(1,1)"
            parts = [f"    [{name}] {typ}"]
            if notnull or pk:
                parts.append("NOT NULL")
            if dflt is not None and str(dflt).upper() not in ("CURRENT_TIMESTAMP",):
                parts.append(f"DEFAULT {dflt}")
            lines.append(" ".join(parts))
        if pk_cols:
            lines.append(
                "    PRIMARY KEY (" + ", ".join(f"[{p}]" for p in pk_cols) + ")"
            )
        for group in unique_groups:
            if group == pk_cols:
                continue  # already enforced by the primary key
            cols_sql = ", ".join(f"[{g}]" for g in group)
            lines.append(f"    UNIQUE ({cols_sql})")
        body = ",\n".join(lines)
        out.append(
            f"IF NOT EXISTS (SELECT 1 FROM sys.tables t JOIN sys.schemas s\n"
            f"    ON s.schema_id = t.schema_id WHERE s.name='dbo' AND t.name='{t}')\n"
            f"CREATE TABLE dbo.[{t}] (\n{body}\n);"
        )
    return "\n\n".join(out), tables


if __name__ == "__main__":
    ddl, tables = convert("project_data.db")
    open(sys.argv[1], "w", encoding="utf-8").write(ddl + "\n")
    print(f"generated DDL for {len(tables)} tables -> {sys.argv[1]}")
