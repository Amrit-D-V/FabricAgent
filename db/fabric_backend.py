"""Fabric SQL Database backend for app storage.

Mirrors the `_SqliteConn` / `_SqliteCursor` interface in db/database.py exactly
— execute() returning a cursor, fetchone() -> dict | None, fetchall() ->
list[dict], plus rowcount / lastrowid / commit() / close() — so the ~47 modules
importing db.database don't change when the backend swaps underneath them.

Scope today is the app_users spike (see migrate_users_spike()). The remaining 16
tables stay on SQLite until the latency numbers justify moving them.

Two notes on dialect, both of which the older scripts/migrate_db_code.py got
wrong and which are the reason this is hand-written rather than regex-generated:

  * T-SQL has no `CREATE TABLE IF NOT EXISTS`; existence is checked against
    sys.tables instead.
  * `SELECT @@IDENTITY` leaks identities across scopes and triggers.
    SCOPE_IDENTITY() is the correct primitive for lastrowid.
"""

from __future__ import annotations

import logging
import os
import re
import threading

_log = logging.getLogger(__name__)

SQL_COPT_SS_ACCESS_TOKEN = 1256


def _server_and_db() -> tuple[str, str]:
    server = os.getenv("FABRIC_APP_DB_SERVER", "").strip()
    database = os.getenv("FABRIC_APP_DB_NAME", "").strip()
    if not server or not database:
        raise RuntimeError(
            "FABRIC_APP_DB_SERVER and FABRIC_APP_DB_NAME must be set in .env"
        )
    return server, database


def _friendlier(e: Exception) -> Exception:
    msg = str(e)
    low = msg.lower()

    if "im002" in low or "data source name not found" in low:
        try:
            import pyodbc

            installed = ", ".join(pyodbc.drivers()) or "(none)"
        except Exception:
            installed = "(could not enumerate)"
        return RuntimeError(
            "ODBC Driver 18 for SQL Server is not installed.\n"
            "  Install:   winget install Microsoft.msodbcsql.18\n"
            "  or MSI:    https://learn.microsoft.com/sql/connect/odbc/"
            "download-odbc-driver-for-sql-server\n"
            f"  Drivers currently visible to pyodbc: {installed}\n"
            f"--- original ---\n{msg}"
        )

    if "40532" in msg or "login failed" in low:
        client_id = os.getenv("AZURE_CLIENT_ID", "(AZURE_CLIENT_ID unset)")
        return RuntimeError(
            "Login failed — the service principal authenticated but has no "
            "access to this database.\n"
            "  Grant it, as a SQL admin, in the target DB:\n"
            f"    CREATE USER [{client_id}] FROM EXTERNAL PROVIDER;\n"
            f"    ALTER ROLE db_datareader ADD MEMBER [{client_id}];\n"
            f"    ALTER ROLE db_datawriter ADD MEMBER [{client_id}];\n"
            "  A Fabric SQL database also auto-pauses when idle; if it was "
            "asleep, retry once after ~30s.\n"
            f"--- original ---\n{msg}"
        )

    # The endpoint accepted the TCP connection and then reset it during the
    # handshake. Seen when the Fabric SQL database is asleep, resuming, or the
    # capacity behind it is throttling -- none of which the caller can fix, and
    # all of which are transient. Typed so the API answers 503 rather than
    # letting a raw pyodbc error escape as an unhandled 500.
    if "08s01" in low or "10054" in msg or "communication link failure" in low:
        from core.errors import ServiceUnavailable

        return ServiceUnavailable(
            "FabricBI's database is not reachable right now. The Fabric SQL "
            "database is asleep, resuming, or its capacity is throttling. "
            "This usually clears on its own -- try again shortly."
        )

    return e


# --- SQLite -> T-SQL statement translation -----------------------------------
# db/database.py is written in one dialect (SQLite). Rewriting its 45 call sites
# to speak both would leave the SQLite path carrying T-SQL scars, so the
# translation lives here, in the adapter that actually needs it.
#
# Only constructs that genuinely appear in database.py are handled. Anything
# unrecognised is passed through untouched rather than guessed at -- a wrong
# rewrite that still parses is far worse than a clear syntax error.
_LIMIT_RE = re.compile(r"\s+LIMIT\s+(\?|\d+)\s*$", re.I)

# SQLite's NOCASE is a built-in collation name that T-SQL does not know; it
# fails with "Invalid collation 'NOCASE'. (448)" rather than being ignored.
#
# The explicit CI collation is emitted rather than COLLATE DATABASE_DEFAULT.
# They behave identically on the current database -- its default really is
# SQL_Latin1_General_CP1_CI_AS -- but DATABASE_DEFAULT inherits whatever the
# database was created with, and a Fabric Warehouse defaults to
# Latin1_General_100_BIN2_UTF8, which is case SENSITIVE. That would turn a
# case-insensitive sort into a case-sensitive one silently, with no error to
# notice. Naming the collation keeps NOCASE meaning what it says.
_COLLATE_NOCASE_RE = re.compile(r"\bCOLLATE\s+NOCASE\b", re.I)
_CI_COLLATION = "COLLATE SQL_Latin1_General_CP1_CI_AS"

# SQLite's `x IS ?` is null-safe equality: true when both sides are NULL, where
# `=` would yield NULL and the row would be skipped. T-SQL has no such operator
# -- `IS` there is only ever `IS NULL` / `IS NOT NULL` -- so `metadata IS @P3`
# fails with "Incorrect syntax near '@P3'" instead of comparing anything.
#
# The rewrite is `EXISTS (SELECT col INTERSECT SELECT ?)`, T-SQL's null-safe
# comparison: INTERSECT treats two NULLs as equal, and the FROM-less SELECT
# resolves `col` against the outer row. The obvious alternative,
# `(col = ? OR (col IS NULL AND ? IS NULL))`, would spend the placeholder twice
# and silently misalign every parameter after it.
#
# Only the parameter forms match, so IS NULL and IS NOT NULL pass through
# untouched. This drives optimistic concurrency in _update_project_metadata:
# write the metadata back only if nobody moved it, where "it was NULL and still
# is" has to count as unchanged.
_IDENT = r"(?:\[[^\]]+\]|[A-Za-z_]\w*)(?:\.(?:\[[^\]]+\]|[A-Za-z_]\w*))*"
_IS_NOT_PARAM_RE = re.compile(rf"\b({_IDENT})\s+IS\s+NOT\s+\?", re.I)
_IS_PARAM_RE = re.compile(rf"\b({_IDENT})\s+IS\s+\?", re.I)

# The parameter is CAST, not the column. pyodbc binds a long Python string as
# SQL_WLONGVARCHAR, which arrives as ntext -- and ntext is not comparable, so
# INTERSECT rejects it with "cannot be used as an operand" even though the
# column itself is nvarchar. Casting the column instead would be wrong: it
# would turn an integer comparison into a string one, whereas casting only the
# parameter leaves the column's own type to decide the comparison.
_NULL_SAFE_OPERAND = "CAST(? AS nvarchar(max))"
_IS_NULL_SAFE = rf"EXISTS (SELECT \1 INTERSECT SELECT {_NULL_SAFE_OPERAND})"
_IS_NOT_NULL_SAFE = rf"NOT EXISTS (SELECT \1 INTERSECT SELECT {_NULL_SAFE_OPERAND})"

# SQLite spells "insert, or update it if it already exists" two ways and T-SQL
# has neither. SQL Server uses MERGE, which needs a target, a source, a join key
# and separate matched / not-matched branches.
#
# ON CONFLICT is mechanically translatable because the statement *declares* its
# conflict key. INSERT OR REPLACE does not, so the key is read from the table's
# primary key and cached.
_ON_CONFLICT_RE = re.compile(
    r"^\s*INSERT\s+INTO\s+(?P<table>\w+)\s*\((?P<cols>[^)]*)\)\s*"
    r"VALUES\s*\((?P<vals>[^)]*)\)\s*"
    r"ON\s+CONFLICT\s*\((?P<keys>[^)]*)\)\s*DO\s+UPDATE\s+SET\s+(?P<sets>.+)$",
    re.I | re.S,
)
_INSERT_REPLACE_RE = re.compile(
    r"^\s*INSERT\s+OR\s+REPLACE\s+INTO\s+(?P<table>\w+)\s*\((?P<cols>[^)]*)\)\s*"
    r"VALUES\s*\((?P<vals>[^)]*)\)\s*$",
    re.I | re.S,
)

_pk_cache: dict[str, list[str]] = {}


def _split(text: str) -> list[str]:
    return [p.strip() for p in text.split(",") if p.strip()]


def _merge(table, cols, vals, keys, sets):
    """Build a MERGE equivalent to a SQLite upsert.

    Parameter order is preserved: the USING clause lists the values in their
    original order, so the caller's existing `?` bindings still line up. Values
    may be placeholders or literals -- fabric_connection passes a literal 1 as
    its id alongside nine placeholders.
    """
    using = ", ".join(f"{v} AS [{c}]" for c, v in zip(cols, vals))
    on = " AND ".join(f"tgt.[{k}] = src.[{k}]" for k in keys)
    if sets is None:
        # INSERT OR REPLACE names no columns to update, so refresh every
        # non-key column -- that is what REPLACE does in SQLite.
        assigns = ", ".join(f"[{c}] = src.[{c}]" for c in cols if c not in keys)
    else:
        assigns = re.sub(r"\bexcluded\.", "src.", sets, flags=re.I)
    insert_cols = ", ".join(f"[{c}]" for c in cols)
    insert_vals = ", ".join(f"src.[{c}]" for c in cols)
    matched = f"WHEN MATCHED THEN UPDATE SET {assigns} " if assigns else ""
    return (
        f"MERGE dbo.[{table}] AS tgt "
        f"USING (SELECT {using}) AS src "
        f"ON {on} "
        f"{matched}"
        f"WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals});"
    )


def _primary_key(cursor, table: str) -> list[str]:
    if table not in _pk_cache:
        cursor.execute(
            "SELECT c.name FROM sys.indexes i "
            "JOIN sys.index_columns ic ON ic.object_id = i.object_id "
            "  AND ic.index_id = i.index_id "
            "JOIN sys.columns c ON c.object_id = i.object_id "
            "  AND c.column_id = ic.column_id "
            "WHERE i.is_primary_key = 1 AND i.object_id = OBJECT_ID(?) "
            "ORDER BY ic.key_ordinal",
            (f"dbo.{table}",),
        )
        _pk_cache[table] = [r[0] for r in cursor.fetchall()]
    return _pk_cache[table]


def _to_tsql(sql: str, cursor=None) -> str:
    """Rewrite the SQLite-only syntax database.py emits.

    `LIMIT n` -> `OFFSET 0 ROWS FETCH NEXT n ROWS ONLY`, which T-SQL allows only
    after an ORDER BY. All four LIMIT sites in database.py already order their
    results, so this is safe; if one ever does not, SQL Server raises rather
    than silently returning an arbitrary row.

    `COLLATE NOCASE` becomes an explicit case-insensitive collation. This runs
    first, and on every statement, because a collation can appear anywhere a
    comparison or sort does -- unlike LIMIT, it is not tied to one clause.

    `x IS ?` becomes an INTERSECT existence test, T-SQL's null-safe equality.

    Upserts become MERGE. Anything unrecognised is returned untouched.
    """
    sql = _COLLATE_NOCASE_RE.sub(_CI_COLLATION, sql)
    # NOT first: otherwise `IS ?` never sees the NOT form's operand.
    sql = _IS_NOT_PARAM_RE.sub(_IS_NOT_NULL_SAFE, sql)
    sql = _IS_PARAM_RE.sub(_IS_NULL_SAFE, sql)

    m = _ON_CONFLICT_RE.match(sql)
    if m:
        return _merge(
            m.group("table"),
            _split(m.group("cols")),
            _split(m.group("vals")),
            _split(m.group("keys")),
            m.group("sets"),
        )

    m = _INSERT_REPLACE_RE.match(sql)
    if m and cursor is not None:
        table = m.group("table")
        keys = _primary_key(cursor, table)
        if keys:
            return _merge(
                table, _split(m.group("cols")), _split(m.group("vals")), keys, None
            )

    m = _LIMIT_RE.search(sql)
    if m:
        sql = _LIMIT_RE.sub(f" OFFSET 0 ROWS FETCH NEXT {m.group(1)} ROWS ONLY", sql)
    return sql


# SQLSTATEs meaning "this handle is no longer usable", as opposed to "your SQL
# was wrong". Fabric SQL drops idle connections and auto-pauses the database,
# so a pooled handle going dead is routine here, not exceptional.
_DEAD_LINK_SQLSTATES = frozenset({"08S01", "08001", "08003", "08007", "HYT00"})


def _is_dead_link(exc: Exception) -> bool:
    code = str(exc.args[0]) if exc.args else ""
    if code in _DEAD_LINK_SQLSTATES:
        return True
    low = str(exc).lower()
    return "communication link failure" in low or "forcibly closed" in low


class FabricCursor:

    def __init__(self, cur, owner=None):
        self._c = cur
        # FabricConn owner for reconnecting dead links in place
        self._owner = owner
        self._lastrowid = None
        self._insert_rowcount = None

    def execute(self, sql, parameters=()):
        self._lastrowid = None
        self._insert_rowcount = None

        def statement():
            translated = _to_tsql(sql, self._c)
            # SCOPE_IDENTITY is batch-scoped, even on the same ODBC cursor.
            # Upserts translate to MERGE and do not use lastrowid.
            if re.match(r"^\s*INSERT\s+INTO\b", translated, re.I):
                translated = translated.rstrip().rstrip(";") + (
                    ";\nSELECT CAST(SCOPE_IDENTITY() AS BIGINT) AS [__fabricbi_inserted_id]"
                )
                return translated, True
            return translated, False

        try:
            batch, capture_id = statement()
            self._c.execute(batch, parameters)
        except Exception as e:
            if self._owner is None or not _is_dead_link(e):
                raise
            self._c = self._owner.reconnect()
            batch, capture_id = statement()
            self._c.execute(batch, parameters)
        if capture_id:
            self._capture_insert_id()
        return self

    def _capture_insert_id(self):
        self._insert_rowcount = self._c.rowcount
        # INSERT row counts and trigger results can precede our SELECT.
        while self._cols() != ["__fabricbi_inserted_id"]:
            if not self._c.nextset():
                raise RuntimeError("Fabric INSERT returned no identity result set")
        row = self._c.fetchone()
        self._lastrowid = int(row[0]) if row and row[0] is not None else None
        # Finish the batch before another statement/commit uses this connection.
        while self._c.nextset():
            pass

    def _cols(self) -> list[str]:
        return [d[0] for d in self._c.description] if self._c.description else []

    def fetchone(self):
        row = self._c.fetchone()
        return dict(zip(self._cols(), row)) if row else None

    def fetchall(self):
        cols = self._cols()
        return [dict(zip(cols, r)) for r in self._c.fetchall()]

    @property
    def rowcount(self):
        return self._c.rowcount if self._insert_rowcount is None else self._insert_rowcount

    @property
    def lastrowid(self):
        return self._lastrowid


class FabricConn:
    def __init__(self, conn):
        self._c = conn

    def cursor(self) -> FabricCursor:
        return FabricCursor(self._c.cursor(), self)

    def reconnect(self):
        self._c = reset_connection()
        return self._c.cursor()

    def execute(self, sql, parameters=()) -> FabricCursor:
        return self.cursor().execute(sql, parameters)

    def commit(self):
        self._c.commit()

    def close(self):
        try:
            self._c.rollback()
        except Exception:
            pass


_thread_local = threading.local()


def _open_connection(server: str, database: str):
    from fabric.fabric_sql import _connect_service_principal

    try:
        return _connect_service_principal(server, database)
    except Exception as e:
        raise _friendlier(e) from e


def reset_connection():
    server, database = _server_and_db()
    stale = getattr(_thread_local, "conn", None)
    _thread_local.conn = None
    if stale is not None:
        try:
            stale.close()
        except Exception as exc:  # noqa: BLE001 - the socket is already gone
            # Expected: closing rolls back, and rollback on a severed link
            # throws. Logged rather than passed so a *different* failure here
            # is still visible.
            _log.debug("discarding dead Fabric SQL handle: %s", exc)
    conn = _open_connection(server, database)
    _thread_local.conn = conn
    _thread_local.key = f"{server}/{database}"
    return conn


def get_connection() -> FabricConn:
    server, database = _server_and_db()
    key = f"{server}/{database}"

    conn = getattr(_thread_local, "conn", None)
    if conn is None or getattr(_thread_local, "key", None) != key:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

        conn = _open_connection(server, database)
        _thread_local.conn = conn
        _thread_local.key = key

    return FabricConn(conn)


APP_USERS_DDL = """
IF NOT EXISTS (
    SELECT 1 FROM sys.tables t
    JOIN sys.schemas s ON s.schema_id = t.schema_id
    WHERE s.name = 'dbo' AND t.name = 'app_users'
)
CREATE TABLE dbo.app_users (
    email         NVARCHAR(320) NOT NULL PRIMARY KEY,
    password_hash NVARCHAR(255) NOT NULL,
    role          NVARCHAR(50)  NOT NULL DEFAULT 'editor',
    created_at    NVARCHAR(64)  NULL,
    is_active     BIT           NOT NULL DEFAULT 1
)
"""


def init_app_users() -> None:
    conn = get_connection()
    try:
        conn.cursor().execute(APP_USERS_DDL)
        conn.commit()
    finally:
        conn.close()


def list_app_users() -> list[dict]:
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT email, role, is_active, created_at "
            "FROM dbo.app_users ORDER BY created_at"
        ).fetchall()
    finally:
        conn.close()


def get_app_user(email: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT email, password_hash, role, is_active "
            "FROM dbo.app_users WHERE email = ?",
            (email.lower().strip(),),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    row["is_active"] = bool(row["is_active"])
    return row


def upsert_app_user(
    email: str, password_hash: str, role: str, created_at: str, is_active: bool
) -> None:
    conn = get_connection()
    try:
        conn.cursor().execute(
            """
            MERGE dbo.app_users AS tgt
            USING (SELECT ? AS email, ? AS password_hash, ? AS role,
                          ? AS created_at, ? AS is_active) AS src
                ON tgt.email = src.email
            WHEN MATCHED THEN UPDATE SET
                password_hash = src.password_hash,
                role          = src.role,
                created_at    = src.created_at,
                is_active     = src.is_active
            WHEN NOT MATCHED THEN INSERT
                (email, password_hash, role, created_at, is_active)
                VALUES (src.email, src.password_hash, src.role,
                        src.created_at, src.is_active);
            """,
            (
                email.lower().strip(),
                password_hash,
                role,
                created_at,
                1 if is_active else 0,
            ),
        )
        conn.commit()
    finally:
        conn.close()
