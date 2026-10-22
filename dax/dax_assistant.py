from __future__ import annotations

import re

from llm.llm_client import stream_text
from model.coverage_report import extract_measure_names
from model.dax_preflight import extract_references
from util.json_utils import strip_json_fences

DAX_SYSTEM = (
    "You are a Power BI DAX expert. Given a question and the tables and measures "
    "of a semantic model, output a SINGLE valid DAX query that answers it.\n"
    "Rules:\n"
    "- Use ONLY the listed tables/columns/measures — never invent names.\n"
    "- The query MUST start with EVALUATE.\n"
    "- Reference measures as [Measure Name]; wrap dotted/spaced table names in single quotes.\n"
    "- Prefer SUMMARIZECOLUMNS for grouped results; TOPN for 'top N'.\n"
    "- Output ONLY the DAX query — no prose, no markdown fences, no explanation."
)


def _table_names(tmdl_files: dict[str, str]) -> list[str]:
    names = []
    for path, text in tmdl_files.items():
        if not path.startswith("tables/"):
            continue
        m = re.search(r"^table\s+(?:'([^']+)'|(\S+))", text, re.MULTILINE)
        if m:
            names.append(m.group(1) or m.group(2))
    return names


def build_grounding(tmdl_files: dict[str, str], max_measures: int = 120) -> str:
    tables = _table_names(tmdl_files)
    measures = extract_measure_names(tmdl_files)
    lines = [f"TABLES ({len(tables)}): " + ", ".join(tables)]
    shown = measures[:max_measures]
    extra = (
        f"  (+{len(measures) - len(shown)} more)" if len(measures) > len(shown) else ""
    )
    lines.append(f"MEASURES ({len(measures)}): " + ", ".join(shown) + extra)
    return "\n".join(lines)


def build_user_prompt(question: str, grounding: str) -> str:
    return (
        f"Semantic model:\n{grounding}\n\n"
        f"Question: {question.strip()}\n\n"
        "Return one DAX EVALUATE query that answers it."
    )


def _clean_dax(text: str) -> str:
    text = strip_json_fences(text) if text.strip().startswith("```") else text
    text = re.sub(
        r"^```[a-zA-Z]*\n?|```$", "", text.strip(), flags=re.MULTILINE
    ).strip()
    return text


def check_query_references(dax: str, tmdl_files: dict[str, str]) -> list[str]:
    cols_by_table: dict[str, set[str]] = {}
    for path, text in tmdl_files.items():
        if not path.startswith("tables/"):
            continue
        m = re.search(r"^table\s+(?:'([^']+)'|(\S+))", text, re.MULTILINE)
        if not m:
            continue
        cols_by_table[m.group(1) or m.group(2)] = {
            (c.group(1) or c.group(2)).strip()
            for c in re.finditer(
                r"^\s+column\s+(?:'([^']+)'|(\S+))", text, re.MULTILINE
            )
        }
    model_measures = set(extract_measure_names(tmdl_files))
    if not cols_by_table and not model_measures:
        return []
    known_measures = model_measures | {  # measures the query itself DEFINEs
        m.group(1).strip()
        for m in re.finditer(
            r"\bMEASURE\s+(?:'[^']+'|[\w.]+)\s*\[([^\[\]]+)\]\s*=",
            dax or "",
            re.IGNORECASE,
        )
    }
    aliases = {  # string literals double as query-local column names
        m.group(1) for m in re.finditer(r'"((?:[^"]|"")*)"', dax or "")
    }

    unknown: list[str] = []
    q_cols, q_meas = extract_references(dax or "")
    for table, col in dict.fromkeys(q_cols):
        if col in known_measures:
            continue  # Table[Measure] is legal DAX
        if table not in cols_by_table or col not in cols_by_table[table]:
            unknown.append(f"{table}[{col}]")
    for name in dict.fromkeys(q_meas):
        if name not in known_measures and name not in aliases:
            unknown.append(f"[{name}]")
    return unknown


def generate_dax(question: str, tmdl_files: dict[str, str]) -> str:
    grounding = build_grounding(tmdl_files)
    user = build_user_prompt(question, grounding)
    chunks: list[str] = []
    for piece in stream_text(
        DAX_SYSTEM, user, max_tokens=2000, agent="default", temperature=0
    ):
        if isinstance(piece, str):
            chunks.append(piece)
    query = _clean_dax("".join(chunks))
    unknown = check_query_references(query, tmdl_files)
    if unknown:
        query += (
            "\n\n-- WARNING: query references objects not found in the model: "
            + ", ".join(unknown)
            + " - verify before running."
        )
    return query


def rewrite_dax_fix(query: str, error_context: str = "") -> str:
    """Rewrite a DAX query/expression to eliminate smells, anti-patterns, and errors."""
    from model.dax_analyze import dax_smells, optimize_dax
    notes = optimize_dax(query)

    prompt = (
        "You are a Power BI DAX performance and correctness expert. Rewrite the given DAX expression "
        "to fix all anti-patterns, syntax errors, and runtime smells.\n\n"
        f"Original DAX:\n{query}\n\n"
        f"Detected Smells:\n{notes}\n"
    )
    if error_context:
        prompt += f"\nRuntime Execution Error:\n{error_context}\n"
    prompt += (
        "\nRules:\n"
        "- Use DIVIDE(num, den, [alternate]) instead of bare '/' operators to avoid division-by-zero errors.\n"
        "- Avoid wrapping tables in FILTER() inside CALCULATE() when a simple column predicate works.\n"
        "- Replace COUNTROWS(FILTER(Table, Cond)) with CALCULATE(COUNTROWS(Table), Cond).\n"
        "- Replace IF(ISBLANK(x), y, x) with COALESCE(x, y).\n"
        "- Remove redundant or empty CALCULATE() wrappers.\n"
        "- Return ONLY the rewritten valid DAX expression/query. No markdown ticks, no commentary."
    )

    try:
        chunks: list[str] = []
        for piece in stream_text(
            DAX_SYSTEM, prompt, max_tokens=2000, agent="default", temperature=0
        ):
            if isinstance(piece, str):
                chunks.append(piece)
        clean = _clean_dax("".join(chunks))
        if clean.strip():
            return clean
    except Exception:
        pass

    # Deterministic fallback when LLM is offline
    fixed = query
    # Replace bare measure division [A] / [B] with DIVIDE([A], [B])
    fixed = re.sub(
        r"(\[[^\]]+\])\s*/\s*(\[[^\]]+\])",
        r"DIVIDE(\1, \2)",
        fixed,
    )
    # Remove empty CALCULATE(expr)
    fixed = re.sub(
        r"\bCALCULATE\s*\(\s*([^,\(\)]+)\s*\)",
        r"\1",
        fixed,
    )
    return fixed

