from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class EvalResult:
    measure_name: str
    table_name: str
    score: float
    issues: list[str] = field(default_factory=list)
    passed: bool = True


def _balanced_parens(expr: str) -> bool:
    depth = 0
    for ch in expr:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _check_measure_syntax(name: str, table: str, expr: str) -> EvalResult:
    issues: list[str] = []
    score = 1.0

    if not expr.strip():
        return EvalResult(name, table, 0.0, ["Empty expression"], False)

    if not _balanced_parens(expr):
        issues.append("Unbalanced parentheses")
        score -= 0.4

    has_var = bool(re.search(r"\bVAR\b", expr, re.IGNORECASE))
    has_return = bool(re.search(r"\bRETURN\b", expr, re.IGNORECASE))
    if has_var and not has_return:
        issues.append("VAR block without RETURN")
        score -= 0.4
    if has_return and not has_var:
        issues.append("RETURN without VAR")
        score -= 0.2

    for m in re.finditer(
        r"DATEADD\s*\([^,]+,\s*-?\d+\s*,\s*(\w+)", expr, re.IGNORECASE
    ):
        grain = m.group(1).upper()
        if grain not in {"DAY", "MONTH", "QUARTER", "YEAR"}:
            issues.append(f"DATEADD uses unknown granularity: {grain}")
            score -= 0.2

    for m in re.finditer(r"CALCULATE\s*\(\s*\)", expr, re.IGNORECASE):
        issues.append("Empty CALCULATE() call")
        score -= 0.3

    score = max(0.0, round(score, 2))
    return EvalResult(name, table, score, issues, passed=score >= 0.6)


def _extract_measures(tmdl_files: dict[str, str]) -> list[tuple[str, str, str]]:
    results = []
    for filename, content in tmdl_files.items():
        current_table = re.search(r"^table\s+(.+)$", content, re.MULTILINE)
        table_name = (
            current_table.group(1).strip().strip("'") if current_table else filename
        )
        for m in re.finditer(
            r"^\s+measure\s+(.+?)\s*=\s*(.*?)"
            r"(?=\n\s+(?:measure|column|hierarchy|partition|formatString|"
            r"lineageTag|displayFolder|description|isHidden|formatStringDefinition|"
            r"annotation|changedProperty)\b|\Z)",
            content,
            re.MULTILINE | re.DOTALL,
        ):
            measure_name = m.group(1).strip().strip("'")
            expr = m.group(2).strip()
            if expr:
                results.append((table_name, measure_name, expr))
    return results


def run_measure_evals(
    tmdl_files: dict[str, str],
    langfuse_tracer: Any | None = None,
    model_name: str = "",
) -> tuple[list[EvalResult], float]:
    measures = _extract_measures(tmdl_files)
    if not measures:
        logger.info("measure_correctness: no measures found in TMDL files")
        return [], 1.0

    results: list[EvalResult] = []
    for table, name, expr in measures:
        r = _check_measure_syntax(name, table, expr)
        results.append(r)
        if langfuse_tracer is not None:
            langfuse_tracer.score(
                name=f"measure.{name}",
                value=r.score,
                comment="; ".join(r.issues) if r.issues else "ok",
            )

    aggregate = round(sum(r.score for r in results) / len(results), 3)

    if langfuse_tracer is not None:
        langfuse_tracer.score(
            name="measure_correctness_aggregate",
            value=aggregate,
            comment=f"{len([r for r in results if not r.passed])} measure(s) failed",
        )

    failed = [r for r in results if not r.passed]
    if failed:
        logger.warning(
            "measure_correctness: %d/%d measures failed (aggregate=%.3f): %s",
            len(failed),
            len(results),
            aggregate,
            [r.measure_name for r in failed],
        )
    return results, aggregate
