"""The SQLite -> T-SQL rewrite that lets db/database.py speak one dialect.

`db/database.py` is written in SQLite and has ~45 call sites. Rather than teach
each of them both dialects, `db/fabric_backend.py::_to_tsql` rewrites the
handful of SQLite-only constructs on the way through the cursor.

Nothing covered this translation, and every gap in it has been found the same
expensive way -- by a user hitting it in the running app:

  * `LIMIT` -> `Incorrect syntax near 'LIMIT'`
  * `ON CONFLICT` / `INSERT OR REPLACE` -> writes failed outright
  * `COLLATE NOCASE` -> `Invalid collation 'NOCASE'. (448)`, which took down
    the whole Fabric panel on Project Management with
    "Could not read saved connections".

These are pure string transformations, so they are testable with no database,
no network and no credentials. That is the point of the file.

The rule for anything unrecognised is pass-through, NOT best-effort guessing: a
wrong rewrite that still parses is far worse than a clear syntax error, because
it runs and returns the wrong rows.
"""

from __future__ import annotations

import pytest

import db.fabric_backend as backend
from db.fabric_backend import _to_tsql


@pytest.fixture(autouse=True)
def _clear_pk_cache():
    """`_pk_cache` is module-level, keyed by table name and never invalidated.

    Without this, one test's fake primary key is served to the next test that
    happens to use the same table name -- which is how the no-primary-key case
    first appeared to fail.
    """
    backend._pk_cache.clear()
    yield
    backend._pk_cache.clear()


class FakeCursor:
    """Stands in for the pyodbc cursor `_to_tsql` uses for primary-key lookup.

    Only INSERT OR REPLACE needs it -- that statement does not declare its own
    conflict key, so the key is read from the table.
    """

    def __init__(self, pk_cols: list[str] | None = None):
        self._pk = pk_cols or []
        self.executed: list[str] = []

    def execute(self, sql, params=()):
        self.executed.append(sql)
        return self

    def fetchall(self):
        return [(c,) for c in self._pk]


# --- COLLATE NOCASE ---------------------------------------------------------


def test_nocase_is_replaced_with_a_case_insensitive_collation():
    out = _to_tsql("SELECT * FROM t ORDER BY name COLLATE NOCASE")
    assert "NOCASE" not in out.upper()
    assert "COLLATE SQL_Latin1_General_CP1_CI_AS" in out


def test_the_real_saved_connections_query_translates():
    """The exact statement from db/database.py:868 that broke the UI."""
    out = _to_tsql(
        "SELECT * FROM fabric_connections "
        "ORDER BY is_default DESC, name COLLATE NOCASE"
    )
    assert out == (
        "SELECT * FROM fabric_connections "
        "ORDER BY is_default DESC, name COLLATE SQL_Latin1_General_CP1_CI_AS"
    )


def test_the_replacement_collation_is_case_insensitive():
    """CI is the whole point. A CS or BIN2 collation would be a silent
    behaviour change, since it still parses and still returns rows."""
    out = _to_tsql("SELECT * FROM t ORDER BY name COLLATE NOCASE")
    collation = out.rsplit("COLLATE ", 1)[1].strip()
    assert "_CI_" in collation, f"{collation} is not case-insensitive"
    assert "DATABASE_DEFAULT" not in collation, (
        "DATABASE_DEFAULT inherits the database's collation, which is BIN2 "
        "(case-sensitive) on a Fabric Warehouse -- NOCASE would stop meaning "
        "case-insensitive with nothing raised."
    )


@pytest.mark.parametrize("spelling", ["COLLATE NOCASE", "collate nocase", "Collate NoCase"])
def test_nocase_is_matched_regardless_of_case(spelling):
    assert "NOCASE" not in _to_tsql(f"SELECT a FROM t ORDER BY a {spelling}").upper()


def test_extra_whitespace_between_the_keywords_is_tolerated():
    assert "NOCASE" not in _to_tsql("SELECT a FROM t ORDER BY a COLLATE   NOCASE").upper()


def test_several_nocase_clauses_are_all_replaced():
    out = _to_tsql(
        "SELECT a FROM t WHERE a = ? COLLATE NOCASE ORDER BY b COLLATE NOCASE"
    )
    assert "NOCASE" not in out.upper()
    assert out.count("SQL_Latin1_General_CP1_CI_AS") == 2


def test_a_column_named_nocase_something_is_not_mangled():
    """The word boundary matters: NOCASE_FLAG is a column, not a collation."""
    sql = "SELECT nocase_flag FROM t"
    assert _to_tsql(sql) == sql


def test_a_collation_that_is_already_valid_tsql_is_left_alone():
    sql = "SELECT a FROM t ORDER BY a COLLATE Latin1_General_CI_AS"
    assert _to_tsql(sql) == sql


# --- LIMIT ------------------------------------------------------------------


def test_a_literal_limit_becomes_offset_fetch():
    out = _to_tsql("SELECT a FROM t ORDER BY a LIMIT 5")
    assert out == "SELECT a FROM t ORDER BY a OFFSET 0 ROWS FETCH NEXT 5 ROWS ONLY"


def test_a_parameterised_limit_keeps_its_placeholder():
    """The `?` must survive, or the caller's binding count no longer matches."""
    out = _to_tsql("SELECT a FROM t ORDER BY a LIMIT ?")
    assert out.endswith("OFFSET 0 ROWS FETCH NEXT ? ROWS ONLY")
    assert out.count("?") == 1


def test_limit_is_only_rewritten_at_the_end_of_the_statement():
    """`limit` also appears as an ordinary identifier."""
    sql = "SELECT limit_value FROM t WHERE limit_value > 3 ORDER BY id"
    assert _to_tsql(sql) == sql


def test_limit_and_nocase_can_both_appear_in_one_statement():
    out = _to_tsql("SELECT a FROM t ORDER BY a COLLATE NOCASE LIMIT 10")
    assert "NOCASE" not in out.upper()
    assert out.endswith("OFFSET 0 ROWS FETCH NEXT 10 ROWS ONLY")


# --- upserts ----------------------------------------------------------------


def test_on_conflict_becomes_a_merge_keyed_on_the_declared_columns():
    out = _to_tsql(
        "INSERT INTO app_users (email, role) VALUES (?, ?) "
        "ON CONFLICT(email) DO UPDATE SET role = excluded.role"
    )
    assert out.startswith("MERGE dbo.[app_users] AS tgt")
    assert "ON tgt.[email] = src.[email]" in out
    assert "WHEN MATCHED THEN UPDATE SET role = src.role" in out
    assert "WHEN NOT MATCHED THEN INSERT" in out


def test_the_excluded_alias_is_rewritten_to_the_merge_source():
    """SQLite calls the proposed row `excluded`; MERGE calls it `src`. Leaving
    `excluded.` in place parses as a missing table, not as a syntax error."""
    out = _to_tsql(
        "INSERT INTO t (a, b) VALUES (?, ?) "
        "ON CONFLICT(a) DO UPDATE SET b = excluded.b"
    )
    assert "excluded." not in out
    assert "b = src.b" in out


def test_merge_preserves_the_order_of_the_placeholders():
    """USING lists values in their original order so existing `?` bindings
    still line up positionally."""
    out = _to_tsql(
        "INSERT INTO t (a, b, c) VALUES (?, ?, ?) "
        "ON CONFLICT(a) DO UPDATE SET b = excluded.b"
    )
    using = out.split("USING (SELECT ", 1)[1].split(") AS src", 1)[0]
    assert using == "? AS [a], ? AS [b], ? AS [c]"


def test_a_literal_value_alongside_placeholders_survives():
    """fabric_connection passes a literal 1 as its id next to placeholders."""
    out = _to_tsql(
        "INSERT INTO fabric_connection (id, workspace) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET workspace = excluded.workspace"
    )
    assert "1 AS [id]" in out
    assert "? AS [workspace]" in out


def test_insert_or_replace_reads_its_key_from_the_primary_key():
    """It declares no conflict key, so the table's PK is the only source."""
    cur = FakeCursor(["email"])
    out = _to_tsql("INSERT OR REPLACE INTO app_users (email, role) VALUES (?, ?)", cur)
    assert out.startswith("MERGE dbo.[app_users]")
    assert "ON tgt.[email] = src.[email]" in out


def test_insert_or_replace_refreshes_every_non_key_column():
    """That is what REPLACE does in SQLite -- the whole row is replaced."""
    cur = FakeCursor(["id"])
    out = _to_tsql("INSERT OR REPLACE INTO t (id, a, b) VALUES (?, ?, ?)", cur)
    # Scope the check to the SET clause: `[id] = src.[id]` legitimately appears
    # in the ON clause, so searching the whole statement proves nothing.
    sets = out.split("WHEN MATCHED THEN UPDATE SET ", 1)[1].split(" WHEN NOT", 1)[0]
    assert "[a] = src.[a]" in sets and "[b] = src.[b]" in sets
    assert "[id]" not in sets, "the join key must not be updated"


def test_insert_or_replace_is_left_alone_when_the_table_has_no_primary_key():
    """No key means no correct MERGE. Pass through and let SQL Server object,
    rather than invent a join condition."""
    cur = FakeCursor([])
    sql = "INSERT OR REPLACE INTO t (a) VALUES (?)"
    assert _to_tsql(sql, cur) == sql


def test_insert_or_replace_is_left_alone_without_a_cursor():
    sql = "INSERT OR REPLACE INTO t (a) VALUES (?)"
    assert _to_tsql(sql) == sql


# --- pass-through -----------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "SELECT * FROM dbo.projects WHERE id = ?",
        "UPDATE t SET a = ? WHERE id = ?",
        "DELETE FROM t WHERE id = ?",
        "MERGE dbo.t AS tgt USING (SELECT ? AS a) AS src ON tgt.a = src.a;",
    ],
)
def test_ordinary_statements_are_returned_untouched(sql):
    assert _to_tsql(sql) == sql
