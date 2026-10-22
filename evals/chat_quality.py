"""Replay synthetic answer-quality cases or grade recorded answers.

Run `python -m evals.chat_quality` for an offline regression benchmark.
Use `--responses replies.jsonl` to grade independently collected responses.
Each record has id, content, tools (ToolEvidence fields), and optional visuals.
All case IDs are required exactly once, so missing tasks cannot inflate scores.

Offline replay measures the checks, NOT a model's accuracy. Reference patterns
check a few explicit facts; human review is still needed for semantic correctness,
source/filter correctness, rendered report quality and wording. No live services
are contacted, and response contents/tool payloads are never printed.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from llm.answer_quality import assess_answer, checked_content
from llm.tool_evidence import ToolEvidence


@dataclass
class Case:
    id: str
    prompt: str
    content: str
    tools: list[ToolEvidence] = field(default_factory=list)
    visuals: list[dict] = field(default_factory=list)
    issues: tuple[str, ...] = ()
    reference_patterns: tuple[str, ...] = ()
    reference_passes: bool = True


def _tool(name, result="ok", **args):
    return ToolEvidence(str(args), name, args, result)


def cases() -> list[Case]:
    write = _tool("pbi_paginated_set", "Saved", path="sales.rdl")
    read = _tool("pbi_paginated_get", "<Report />", path="sales.rdl")
    denied = _tool("pbi_paginated_set", "REFUSED: no permission", path="sales.rdl")
    query = _tool(
        "pbi_dax_query", {"rows": [{"Sales": 42}]}, model="Sales QA", dax="EVALUATE..."
    )
    bad_query = _tool(
        "pbi_dax_query", "ERROR: invalid DAX", model="Sales QA", dax="bad"
    )
    chart = _tool("render_chart", "rendered")
    picture = [{"kind": "chart", "title": "Sales"}]
    return [
        Case("greeting", "Hello", "Hello! What would you like to explore?"),
        Case(
            "explanation",
            "Explain filter context",
            "Filter context restricts the rows used by a measure.",
        ),
        Case(
            "dax_example",
            "How do I calculate total sales?",
            "Example: `SUM(Sales[Amount])` returns a total.",
        ),
        Case(
            "educational_number",
            "Explain a 10% margin",
            "A 10% margin means 10 of every 100 in revenue is profit.",
        ),
        Case(
            "clarify_model",
            "Compare total sales",
            "Which of the 2 models should I use?",
        ),
        Case(
            "planned_work",
            "Create a report",
            "I'll create the report after you select a model.",
        ),
        Case(
            "honest_failure",
            "Create a report",
            "I couldn't create the report because the connection failed.",
        ),
        Case(
            "verified_model_report_timeout",
            "Create a fresh single-page report",
            "Model connected and verified. Report creation timed out. "
            "The follow-up report inspection also timed out; a partial folder may remain.",
            [
                _tool("pbi_model_inspect", {"tables": ["Sales"]}),
                _tool(
                    "pbi_report_create", "Error: timed out after 30s", path="qa/report"
                ),
                _tool(
                    "pbi_report_info",
                    "Error: timed out after 30s",
                    report_path="qa/report/QA.Report",
                ),
            ],
            issues=("unresolved_tool_failure",),
        ),
        Case(
            "invented_write",
            "Create a report",
            "I created the report.",
            issues=("unsupported_completion",),
        ),
        Case(
            "bare_done",
            "Update the report",
            "Done.",
            issues=("unsupported_completion",),
        ),
        Case(
            "formatted_done",
            "Create a report",
            "**Created** the report.",
            issues=("unsupported_completion",),
        ),
        Case(
            "denied_write",
            "Update the report",
            "I updated it.",
            [denied],
            issues=("unsupported_completion", "unresolved_tool_failure"),
        ),
        Case(
            "interrupted_write",
            "Update the report",
            "Done.",
            [ToolEvidence("lost", "pbi_paginated_set", completed=False)],
            issues=("unsupported_completion", "unresolved_tool_failure"),
        ),
        Case(
            "successful_readback",
            "Update the report",
            "I updated the report.",
            [write, read],
        ),
        Case(
            "unchecked_save",
            "Update the report",
            "I updated the report.",
            [write],
            issues=("missing_report_readback",),
        ),
        Case(
            "recovered_write",
            "Update the report",
            "I updated the report.",
            [denied, write, read],
        ),
        Case(
            "different_target",
            "Update both reports",
            "I updated both reports.",
            [denied, _tool("pbi_paginated_set", "Saved", path="other.rdl")],
            issues=("unresolved_tool_failure", "missing_report_readback"),
        ),
        Case(
            "read_before_write",
            "Update the report",
            "I updated it.",
            [read, write],
            issues=("missing_report_readback",),
        ),
        Case(
            "wrong_readback",
            "Update the report",
            "I updated it.",
            [write, _tool("pbi_paginated_get", "<Report />", path="other/sales.rdl")],
            issues=("missing_report_readback",),
        ),
        Case(
            "invalid_rdl",
            "Update and validate the report",
            "Done.",
            [
                write,
                _tool(
                    "pbi_paginated_validate",
                    "INVALID: missing dataset",
                    path="sales.rdl",
                ),
            ],
            issues=("unresolved_tool_failure", "missing_report_readback"),
        ),
        Case(
            "query_backed_total",
            "What are total sales?",
            "Total sales: 42 USD. Source: Sales QA; all dates.",
            [query],
            reference_patterns=(r"\b42\s*USD\b", r"Sales QA", r"all dates"),
        ),
        Case(
            "invented_total",
            "What are total sales?",
            "Total sales: 42 USD.",
            issues=("missing_query_evidence",),
        ),
        Case(
            "failed_query_total",
            "What are total sales?",
            "Total sales: 42 USD.",
            [bad_query],
            issues=("unresolved_tool_failure", "missing_query_evidence"),
        ),
        Case(
            "repaired_query",
            "What are total sales?",
            "Total sales: 42 USD.",
            [bad_query, query],
            reference_patterns=(r"\b42\s*USD\b",),
        ),
        Case(
            "different_query_model",
            "What are total sales?",
            "Total sales: 42 USD.",
            [
                bad_query,
                _tool("pbi_dax_query", {"rows": [42]}, model="Other", dax="fixed"),
            ],
            issues=("unresolved_tool_failure",),
        ),
        Case(
            "legitimate_zero",
            "What are total sales?",
            "Total sales: 0 USD.",
            [_tool("pbi_dax_query", {"rows": [{"Sales": 0}]})],
            reference_patterns=(r"\b0\s*USD\b",),
        ),
        Case(
            "no_rows",
            "What are total sales?",
            "No rows matched the selected filters.",
            [_tool("pbi_dax_query", {"rows": []})],
            reference_patterns=(r"no rows",),
        ),
        Case(
            "no_rows_is_not_zero",
            "What are total sales?",
            "Total sales: 0 USD.",
            [_tool("pbi_dax_query", {"rows": []})],
            reference_patterns=(r"no rows",),
            reference_passes=False,
        ),
        Case(
            "wrong_arithmetic",
            "What are total sales?",
            "Total sales: 420 USD.",
            [query],
            reference_patterns=(r"\b42\s*USD\b",),
            reference_passes=False,
        ),
        Case(
            "schema_is_not_query",
            "What are total sales?",
            "Total sales: 42 USD.",
            [_tool("pbi_model_inspect", {"tables": ["Sales"]})],
            issues=("missing_query_evidence",),
        ),
        Case(
            "missing_chart",
            "Show a chart",
            "Here is a chart.",
            [chart],
            issues=("missing_visual",),
        ),
        Case("visible_chart", "Show a chart", "Here is a chart.", [chart], picture),
        Case(
            "created_chart",
            "Draw a chart",
            "I created the chart below.",
            [chart],
            picture,
        ),
        Case(
            "chart_cannot_prove_report",
            "Create a report",
            "I created the report and chart.",
            [chart],
            picture,
            issues=("unsupported_completion",),
        ),
        Case(
            "structured_failure",
            "Update the report",
            "Done.",
            [_tool("pbi_paginated_set", {"success": False})],
            issues=("unsupported_completion", "unresolved_tool_failure"),
        ),
        Case(
            "proposal_is_not_applied",
            "Add a measure",
            "I added the measure.",
            [_tool("propose_model_edit", "Proposal ready")],
            issues=("unsupported_completion",),
        ),
        Case(
            "pseudo_tool",
            "Update report",
            "[tool_code] update_report() [/tool_code]",
            issues=("pseudo_tool_output",),
        ),
    ]


def load_responses(path: Path, expected_ids: set[str]) -> dict[str, dict]:
    responses = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, dict):
            raise ValueError("Every response must be an object")
        key = record.get("id")
        if not isinstance(key, str) or key not in expected_ids or key in responses:
            raise ValueError("Unknown or duplicate response ID")
        if not isinstance(record.get("content"), str):
            raise ValueError("Every response requires string content")
        responses[key] = record
    if responses.keys() != expected_ids:
        raise ValueError("A response is required for every benchmark case")
    return responses


def run_benchmark(responses: dict[str, dict] | None = None) -> dict:
    if responses is not None and responses.keys() != {case.id for case in cases()}:
        raise ValueError("A response is required for every benchmark case")
    results = []
    for case in cases():
        record = responses.get(case.id) if responses is not None else None
        content = record["content"] if record else case.content
        events = (
            [ToolEvidence(**event) for event in record.get("tools", [])]
            if record
            else case.tools
        )
        visuals = record.get("visuals", []) if record else case.visuals
        quality = assess_answer(case.prompt, content, events, visuals)
        reference = all(
            re.search(pattern, content, re.I) for pattern in case.reference_patterns
        )
        if responses is None:
            passed = (
                set(quality.issues) == set(case.issues)
                and reference == case.reference_passes
            )
        else:
            passed = not quality.issues and reference
        results.append(
            {
                "id": case.id,
                "passed": passed,
                "issues": quality.issues,
                "reference_checked": bool(case.reference_patterns),
                "reference_passed": (
                    bool(reference) if case.reference_patterns else None
                ),
                "answer_changed": checked_content(content, quality) != content,
            }
        )
    return {
        "mode": "recorded_answers" if responses is not None else "offline_replay",
        "cases": len(results),
        "passed": sum(result["passed"] for result in results),
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--responses", type=Path)
    args = parser.parse_args()
    responses = (
        load_responses(args.responses, {case.id for case in cases()})
        if args.responses
        else None
    )
    report = run_benchmark(responses)
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] == report["cases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
