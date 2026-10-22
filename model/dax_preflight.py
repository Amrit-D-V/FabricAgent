from __future__ import annotations

import difflib
import re

_STRIP = re.compile(
    r'"(?:[^"]|"")*"'  # DAX string literals ("" escapes a quote)
    r"|//[^\n]*|--[^\n]*"  # line comments
    r"|/\*.*?\*/",  # block comments
    re.DOTALL,
)
_QUALIFIED = re.compile(
    r"(?:'(?P<qt>[^']+)'|(?P<t>[A-Za-z_][\w.]*))\s*\[(?P<c>[^\[\]]+)\]"
)
_BARE = re.compile(r"\[(?P<m>[^\[\]]+)\]")

_KEYWORDS = frozenset(
    {
        "return",
        "in",
        "not",
        "and",
        "or",
        "true",
        "false",
        "evaluate",
        "order",
        "by",
        "asc",
        "desc",
        "define",
        "measure",
        "column",
        "table",
        "var",
    }
)


def extract_references(dax: str) -> tuple[list[tuple[str, str]], list[str]]:
    body = _STRIP.sub(" ", dax or "")
    columns: list[tuple[str, str]] = []

    def _swallow(m: re.Match) -> str:
        table = (m.group("qt") or m.group("t") or "").strip()
        if m.group("t") and table.lower() in _KEYWORDS:
            return m.group(0)[len(m.group("t")) :]
        columns.append((table, m.group("c").strip()))
        return " "

    rest = _QUALIFIED.sub(_swallow, body)
    measures = [m.group("m").strip() for m in _BARE.finditer(rest)]
    return columns, measures


def _close(name: str, pool, n: int = 3) -> str:
    hits = difflib.get_close_matches(name, list(pool), n=n, cutoff=0.5)
    return f" Did you mean: {', '.join(hits)}?" if hits else ""


def validate_dax_references(
    dax: str,
    col_reg: dict,
    mea_reg: dict,
    extra_measures: tuple | list = (),
) -> str | None:
    col_keys = {k for k in (col_reg or {}) if k != "_meta"}
    mea_keys = {k for k in (mea_reg or {}) if k != "_meta"}
    if not col_keys and not mea_keys:
        return None

    known_tables = {k.rpartition(".")[0] for k in col_keys | mea_keys if "." in k}
    known_measures = {k.rpartition(".")[2] for k in mea_keys} | {
        str(m) for m in extra_measures
    }

    columns, measures = extract_references(dax)
    problems: list[str] = []

    for table, col in columns:
        if f"{table}.{col}" in col_keys or f"{table}.{col}" in mea_keys:
            continue
        if col in known_measures:
            continue  # Table[Measure] is legal DAX
        if table not in known_tables:
            problems.append(
                f"table '{table}' does not exist in the model."
                + _close(table, known_tables)
            )
        else:
            table_cols = {
                k.rpartition(".")[2] for k in col_keys if k.rpartition(".")[0] == table
            }
            problems.append(
                f"column '{table}[{col}]' does not exist." + _close(col, table_cols)
            )

    for name in measures:
        if name in known_measures:
            continue
        qualified_hit = {
            k.rpartition(".")[0] for k in col_keys if k.endswith(f".{name}")
        }
        if qualified_hit:
            tbl = min(qualified_hit)
            problems.append(
                f"'[{name}]' is a COLUMN, not a measure — qualify it as "
                f"{tbl}[{name}]."
            )
        else:
            problems.append(
                f"measure '[{name}]' does not exist." + _close(name, known_measures)
            )

    body = _STRIP.sub(" ", dax or "")
    if body.count("(") != body.count(")"):
        problems.append(
            f"unbalanced parentheses ({body.count('(')} open vs "
            f"{body.count(')')} close)."
        )

    if not problems:
        return None
    listed = "\n".join(f"  • {p}" for p in dict.fromkeys(problems))
    return (
        "ERROR — the DAX definition references objects that do not exist in "
        f"the connected model, so this edit would fail at apply time:\n{listed}\n"
        "Fix the reference(s) against the model metadata and re-propose."
    )
