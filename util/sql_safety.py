from __future__ import annotations

import re

_FORBIDDEN_NAMES = (
    "Insert",
    "Update",
    "Delete",
    "Merge",
    "Drop",
    "Create",
    "Alter",
    "Command",
    "Execute",
    "Set",
    "Use",
    "TruncateTable",
    "Into",
)

_READONLY_NAMES = (
    "Select",
    "Union",
    "Intersect",
    "Except",
    "Subquery",
    "Query",
)

try:
    import sqlglot
    from sqlglot import expressions as exp

    _FORBIDDEN_NODES = tuple(
        node for name in _FORBIDDEN_NAMES if (node := getattr(exp, name, None)) is not None
    )
    _READONLY_ROOTS = tuple(
        node for name in _READONLY_NAMES if (node := getattr(exp, name, None)) is not None
    )
    _HAS_SQLGLOT = True
except ImportError:
    _FORBIDDEN_NODES = ()
    _READONLY_ROOTS = ()
    _HAS_SQLGLOT = False


_FORBIDDEN_KEYWORDS = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "MERGE",
    "UPSERT",
    "TRUNCATE",
    "DROP",
    "ALTER",
    "CREATE",
    "REPLACE",
    "GRANT",
    "REVOKE",
    "DENY",
    "EXEC",
    "EXECUTE",
    "CALL",
    "INTO",
    "BACKUP",
    "RESTORE",
    "USE",
    "WAITFOR",
    "SHUTDOWN",
    "OPENROWSET",
    "OPENQUERY",
    "OPENDATASOURCE",
)


_RX_BLOCK_COMMENT = re.compile(r"/\*(?:(?!/\*|\*/).)*?\*/", re.DOTALL)
_RX_UNTERMINATED_COMMENT = re.compile(r"/\*.*$", re.DOTALL)
_RX_LINE_COMMENT = re.compile(r"--[^\n]*")
_RX_SINGLE_QUOTES = re.compile(r"'(?:[^']|'')*'")
_RX_BRACKET_ID = re.compile(r"\[[^\]]*\]")
_RX_DOUBLE_QUOTES = re.compile(r'"(?:[^"]|"")*"')


def _strip(sql: str) -> str:
    s = sql
    prev = None
    while prev != s:
        prev = s
        s = _RX_BLOCK_COMMENT.sub(" ", s)
    s = _RX_UNTERMINATED_COMMENT.sub(" ", s)
    s = _RX_LINE_COMMENT.sub(" ", s)
    s = _RX_SINGLE_QUOTES.sub(" ", s)
    s = _RX_BRACKET_ID.sub(" ", s)
    s = _RX_DOUBLE_QUOTES.sub(" ", s)
    return s


def _is_read_only_regex(sql: str) -> tuple[bool, str]:
    cleaned = _strip(sql).strip().rstrip(";").strip()
    if not cleaned:
        return False, "no statement after stripping comments/strings"
    if ";" in cleaned:
        return False, "multiple statements are not allowed"
    if not re.match(r"^(SELECT|WITH)\b", cleaned, re.IGNORECASE):
        return False, "only SELECT/WITH queries are allowed"
    for kw in _FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{kw}\b", cleaned, re.IGNORECASE):
            return False, f"forbidden keyword: {kw}"
    return True, ""


def _is_read_only_sqlglot(sql: str) -> tuple[bool, str]:
    try:
        stmts = [s for s in sqlglot.parse(sql, read="tsql") if s is not None]
    except Exception as e:
        return False, f"unparseable as read-only SQL ({e})"
    if not stmts:
        return False, "empty query"
    if len(stmts) > 1:
        return False, "multiple statements are not allowed"
    root = stmts[0]
    if _FORBIDDEN_NODES:
        bad = next(iter(root.find_all(*_FORBIDDEN_NODES)), None)
        if bad is not None:
            return False, f"non-read-only operation: {type(bad).__name__}"
    if _READONLY_ROOTS and not isinstance(root, _READONLY_ROOTS):
        return (
            False,
            f"only SELECT/WITH/UNION queries are allowed (parsed as {type(root).__name__})",
        )
    return True, ""


def is_read_only_sql(sql: str) -> tuple[bool, str]:
    if not sql or not sql.strip():
        return False, "empty query"
    return _is_read_only_sqlglot(sql) if _HAS_SQLGLOT else _is_read_only_regex(sql)
