"""Conservative checks on web answers against this turn's tool evidence.

These checks detect missing evidence and execution failures, not arbitrary
semantic or numeric correctness. Never label an answer factually verified.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from auth.tool_access import READ_ONLY_TOOLS
from llm.agent_runtime import _READONLY_TOOLS
from llm.claim_guard import claims_change
from llm.tool_evidence import ToolEvidence, bare_tool_name
from model.model_edit_guard import classify_modeling_op, normalize_mcp_request

QUALITY_INSTRUCTIONS = """
Answer quality:
- Inspect the selected model before using unfamiliar fields. Use pbi_model_inspect
  and pbi_dax_query for live data. Never invent figures or treat earlier assistant
  prose as query evidence. Include the source/model, period, units and material
  filters with numerical answers. Distinguish no rows, zero and a failed query.
- Use the project's definitions and approved measures. Ask a short question only
  when an ambiguous model, metric or period would materially change the answer.
- After a failed call, inspect the error and correct the cause before retrying.
  Do not repeat an unchanged failed call in a loop. A failed or interrupted write
  may have partly executed: inspect its target before trying the write again.
- After changing a report, read back the same report and validate it. For a
  deleted report, list its container to confirm absence instead. After a
  model edit, read back the affected object and execute the relevant DAX check.
  Report only changes supported by successful results. A proposal is not an
  applied change. A successful save is not proof of valid bindings or rendering.
- Only say a chart is shown when render_chart/render_map succeeded. Only say a
  report was validated when its validation actually passed. Explain partial
  completion plainly, including what is still unresolved.
- Lead with the answer, then give the minimum evidence and assumptions needed.
""".strip()

_READS = (
    READ_ONLY_TOOLS
    | _READONLY_TOOLS
    | {
        "pbi_report_inspect",
        "pbi_model_inspect",
        "pbi_dax_query",
        "pbi_report_open",
        "pbi_desktop_status",
    }
)
_AUXILIARY = {
    "ToolSearch",
    "list_skills",
    "load_skill",
    "connection_operations",
    "database_operations",
    "propose_model_edit",
}
_QUERIES = {"pbi_dax_query", "dax_query_operations"}
_CODE = re.compile(r"```.*?```", re.S)
_NEGATIVE = re.compile(
    r"\b(?:cannot|can't|couldn't|could not|unable|didn't|did not|not yet|"
    r"haven't|have not|hasn't|has not|wasn't|was not|failed|not completed)\b",
    re.I,
)
_EXPLANATION = re.compile(
    r"^\s*(?:how (?:do|can|would|should|to)|explain|what is (?:a|an)|"
    r"give (?:me )?an? example|write (?:me )?(?:a )?(?:dax|sql)|"
    r"show (?:me )?how)\b",
    re.I,
)
_MUTATION = re.compile(
    r"\b(?:create|build|update|delete|remove|add|rename|fix|save)\b", re.I
)
_DONE = re.compile(r"^\s*(?:done|completed|all set|finished)[.!\s]*$", re.I)
_PAST = re.compile(
    r"^\s*(?:[-*]\s*)?(?:created|updated|deleted|removed|added|renamed|saved|built)\b",
    re.I,
)
_PLAN = re.compile(
    r"\b(?:I will|I'll|I can|we can|we will|would|could|should|if)\b", re.I
)
_METRIC = re.compile(
    r"\b(?:revenue|sales|profit|cost|margin|growth|total|average|count|"
    r"how many|how much|row count|compare|comparison|breakdown)\b",
    re.I,
)
_NUMBER = re.compile(r"(?<![\w])(?:[$€£₹]\s*)?-?\d[\d,.]*(?:\s*%)?")
_CHART_CLAIM = re.compile(
    r"\b(?:here (?:is|are)|shown below|plotted below)\b.*\b(?:chart|map|plot)\b|"
    r"\b(?:chart|map|plot)\b.*\b(?:shown|below|attached|displayed)\b",
    re.I,
)
_READBACKS = {
    "paginated": {"pbi_paginated_get", "pbi_paginated_validate"},
    "report": {
        "pbi_report_info",
        "pbi_report_inspect",
        "pbi_report_validate",
        "pbi_report_health_check",
        "pbi_visual_list",
        "pbi_visual_bindings",
    },
}
_VALIDATORS = {
    "pbi_paginated_validate",
    "pbi_report_validate",
    "pbi_report_health_check",
}
_VALIDATION_CLAIM = re.compile(
    r"\b(?:report|rdl)(?:\s+(?:structure|schema|bindings))?\s+"
    r"(?:(?:is|was|has been|has|now|successfully)\s+)*"
    r"(?:validated|structurally valid|valid|passed validation)\b"
    r"|\bvalidated\s+(?:(?:the|this|current|new|updated|saved)\s+)*(?:report|rdl)\b"
    r"|\b(?:report|rdl)\s+validation\s+(?:passed|succeeded)\b"
    r"|\bverified\s+(?:(?:the|this|current)\s+)*(?:report|rdl)\s+"
    r"(?:structure|schema|bindings)\b",
    re.I,
)
_VALIDATION_SUMMARY = re.compile(r"\b(?:validation passed|all checks passed)\b", re.I)


def _write(event: ToolEvidence) -> bool:
    name = bare_tool_name(event.name)
    if name == "pbi_tmdl_doctor":
        return event.input.get("apply") is True
    if name == "pbi_generate_docs":
        return event.input.get("save") is True
    if name == "pbi_export_model":
        return True
    kind = classify_modeling_op(name, normalize_mcp_request(event.input))
    if kind != "other":
        return kind == "mutate"
    return name.startswith("pbi_") and name not in _READS and name not in _AUXILIARY


def _retry_key(event: ToolEvidence) -> tuple:
    args = event.input
    if bare_tool_name(event.name) in _QUERIES:
        # A repaired query may change DAX, but changing models/workspaces is a
        # different operation. Writes require identical inputs to count as a
        # confirmed retry; a success on another target never clears a failure.
        args = {k: v for k, v in args.items() if k not in {"dax", "query"}}
    return bare_tool_name(event.name), json.dumps(args, sort_keys=True, default=str)


def _unresolved(events: list[ToolEvidence]) -> list[ToolEvidence]:
    latest = {_retry_key(event): event for event in events}
    return [event for event in latest.values() if not event.succeeded]


def _claims_completion(prompt: str, prose: str) -> bool:
    clauses = re.sub(r"\b(?:but|however)\b|;", "\n", prose, flags=re.I)
    for sentence in re.split(r"[.!?\n]+", clauses):
        if _NEGATIVE.search(sentence) or _PLAN.search(sentence):
            continue
        if claims_change(sentence) or _PAST.search(sentence):
            return True
        if _MUTATION.search(prompt) and not _EXPLANATION.search(prompt):
            if _DONE.fullmatch(sentence):
                return True
    return False


def _assertions(prose: str) -> str:
    prose = re.sub(r"(?m)^\s*\d+[.)]\s+", "", prose)
    clauses = re.sub(r"\b(?:but|however)\b|;", "\n", prose, flags=re.I)
    sentences = re.findall(r"[^.!?\n]+[.!?]?", clauses)
    return "\n".join(
        sentence
        for sentence in sentences
        if not sentence.endswith("?")
        and not _NEGATIVE.search(sentence)
        and not _PLAN.search(sentence)
    )


def _claims_report_validation(prompt: str, prose: str) -> bool:
    """A verified connection/existence check is not report validation.

    Keep clauses separate: a model heading followed by a report failure must
    not turn the model's word "verified" into a claim about the report.
    """
    for line in re.split(r"[.!?\n]+", prose):
        if (
            _NEGATIVE.search(line)
            or _PLAN.search(line)
            or re.search(r"\bnot\b", line, re.I)
        ):
            continue
        if _VALIDATION_CLAIM.search(line):
            return True
        if (
            re.search(r"\b(?:report|rdl)\b", prompt, re.I)
            and _VALIDATION_SUMMARY.search(line)
            and not re.search(r"\b(?:model|connection|query)\b", line, re.I)
        ):
            return True
    return False


def _has_unqueried_figures(assertions: str, events: list[ToolEvidence]) -> bool:
    metadata = any(
        event.succeeded and bare_tool_name(event.name) == "pbi_model_inspect"
        for event in events
    )
    # Inline names such as `Forecast 2025` are identifiers, not measured values.
    prose = re.sub(r"`[^`\n]*[A-Za-z][^`\n]*`", "", assertions)
    for clause in re.split(r"[.!?\n]+", prose):
        clause = re.sub(r"^\s*\d+[.)]\s*", "", clause)
        if not _NUMBER.search(clause):
            continue
        if metadata and re.search(
            r"\b(?:tables?|columns?|measures?|relationships?|rows?|pages?|"
            r"visuals?|layout|forecast|rolling|YTD|YoY|MoM)\b",
            clause,
            re.I,
        ):
            # Metadata legitimately contains schema counts and table row counts.
            if not re.search(
                r"\b(?:sales|revenue|profit|margin|cost)\s*(?:is|are|:|=)\s*\d"
                r"|[$€£₹]\s*\d|\d[\d,.]*\s*(?:%|(?:USD|EUR|GBP|INR)\b)",
                clause,
                re.I,
            ):
                continue
        return True
    return False


_ACTIONS = {
    "pbi_report_create": "Report creation",
    "pbi_report_info": "Report inspection",
    "pbi_report_inspect": "Report inspection",
    "pbi_report_validate": "Report validation",
    "pbi_report_health_check": "Report health check",
    "pbi_paginated_validate": "Paginated report validation",
    "pbi_paginated_get": "Paginated report inspection",
    "pbi_paginated_set": "Paginated report update",
    "pbi_paginated_create": "Paginated report creation",
    "pbi_dax_query": "The data query",
}


def _failure_note(event: ToolEvidence) -> str:
    """Describe failure categories without copying credentials or payloads."""
    action = _ACTIONS.get(bare_tool_name(event.name), "An operation")
    if not event.completed:
        return f"{action} returned no confirmation; its outcome is unknown."
    result = str(event.result).lower()
    if "timed out" in result or "timeout" in result:
        note = f"{action} timed out."
        if _write(event):
            note += " Check the target before retrying; the change may be partial."
        return note
    if any(word in result for word in ("denied", "refused", "permission")):
        return f"{action} was denied. Check access to the selected project and source."
    if any(word in result for word in ("credential", "authentication", "unauthorized")):
        return f"{action} could not authenticate. Check the saved source connection."
    if "invalid" in result:
        return f"{action} found invalid input or report content."
    return f"{action} failed or returned no confirmation."


def _needs_visual(prompt: str, assertions: str) -> bool:
    if _CHART_CLAIM.search(assertions):
        return True
    return bool(
        assertions.strip()
        and not _EXPLANATION.search(prompt)
        and re.search(r"\b(?:show|draw|plot|render|create)\b", prompt, re.I)
        and re.search(r"\b(?:chart|map|plot)\b", prompt, re.I)
        and not re.search(r"\b(?:report|rdl)\b", prompt, re.I)
    )


def _report_target(event: ToolEvidence) -> tuple[str, str] | None:
    args = event.input
    name = bare_tool_name(event.name)
    path = args.get("report_path")
    family = "report"
    if name.startswith("pbi_paginated_"):
        path, family = args.get("path"), "paginated"
    elif name == "pbi_report_create":
        path = args.get("path")
    if not isinstance(path, str) or not path:
        return None
    # Keep the full path: reports in different folders can share a filename.
    return family, path.replace("\\", "/").removeprefix("./").rstrip("/")


def _report_checks(events: list[ToolEvidence]) -> tuple[bool, bool]:
    pending = set()
    unvalidated = set()
    has_validation = False
    for event in events:
        if bare_tool_name(event.name) in {"pbi_paginated_delete", "pbi_report_delete"}:
            # A removed file cannot be read back. Its delete result is the
            # execution evidence; validating absence needs a listing check.
            continue
        target = _report_target(event)
        if not event.succeeded or target is None:
            continue
        if _write(event):
            pending.add(target)
            unvalidated.add(target)
        elif bare_tool_name(event.name) in _READBACKS.get(target[0], set()):
            pending.discard(target)
            if bare_tool_name(event.name) in _VALIDATORS:
                unvalidated.discard(target)
                has_validation = True
    return bool(pending), has_validation and not unvalidated


@dataclass
class AnswerQuality:
    issues: list[str] = field(default_factory=list)
    blocking: bool = False
    tool_calls: int = 0
    succeeded: int = 0
    failure_notes: list[str] = field(default_factory=list)

    def public(self) -> dict:
        return {
            "status": "limited" if self.issues else "checked",
            "issues": self.issues,
            "tool_calls": self.tool_calls,
            "succeeded": self.succeeded,
        }


def assess_answer(
    prompt: str,
    content: str,
    events: list[ToolEvidence],
    visuals: list[dict],
) -> AnswerQuality:
    prose = _CODE.sub("", content).replace("**", "")
    assertions = _assertions(prose)
    quality = AnswerQuality(
        tool_calls=len(events),
        succeeded=sum(event.succeeded for event in events),
    )
    completed = _claims_completion(prompt, prose)
    writes = [event for event in events if _write(event)]
    unresolved = _unresolved(events)
    if "[tool_code]" in content or "[/tool_code]" in content:
        quality.issues.append("pseudo_tool_output")
        quality.blocking = True
    rendered_only = (
        bool(visuals)
        and bool(re.search(r"\b(?:chart|map|plot)\b", prose, re.I))
        and not re.search(
            r"\b(?:report|model|measure|page|table)\b", prompt + prose, re.I
        )
    )
    if completed and not rendered_only and not any(event.succeeded for event in writes):
        quality.issues.append("unsupported_completion")
        quality.blocking = True
    if unresolved:
        quality.issues.append("unresolved_tool_failure")
        quality.failure_notes = list(
            dict.fromkeys(_failure_note(event) for event in unresolved)
        )
        if completed and any(
            bare_tool_name(event.name) not in _AUXILIARY for event in unresolved
        ):
            quality.blocking = True
    if (
        _METRIC.search(prompt)
        and not _EXPLANATION.search(prompt)
        and _has_unqueried_figures(assertions, events)
        and not any(
            event.succeeded and bare_tool_name(event.name) in _QUERIES
            for event in events
        )
    ):
        quality.issues.append("missing_query_evidence")
        quality.blocking = True
    if _needs_visual(prompt, assertions) and not visuals:
        quality.issues.append("missing_visual")
        quality.blocking = True
    missing_readback, validated = _report_checks(events)
    if missing_readback:
        quality.issues.append("missing_report_readback")
    if _claims_report_validation(prompt, prose) and not validated:
        quality.issues.append("unsupported_validation")
        quality.blocking = True
    return quality


_NOTES = {
    "pseudo_tool_output": "The proposed tool call was not confirmed as executed.",
    "unsupported_completion": (
        "I couldn't confirm the requested changes from a successful tool result."
    ),
    "unresolved_tool_failure": (
        "Some operations failed or returned no confirmation; "
        "the full request is still unresolved."
    ),
    "missing_query_evidence": "I don't have a successful query result to support those figures.",
    "missing_visual": (
        "The chart wasn't produced successfully, so it isn't available to display."
    ),
    "missing_report_readback": (
        "The change tool reported success, but I haven't confirmed the saved "
        "report with a follow-up check."
    ),
    "unsupported_validation": "I don't have a successful validation result for the current report.",
}


def checked_content(content: str, quality: AnswerQuality) -> str:
    if not quality.issues:
        return content
    notes = []
    for issue in quality.issues:
        if issue == "unresolved_tool_failure" and quality.failure_notes:
            # Keep the model's useful explanation when it already acknowledges
            # the failure. Blocking claims still get concrete fallback details.
            if quality.blocking or not re.search(
                r"\b(?:failed|timed out|timeout|denied|refused|couldn't|"
                r"could not|unable|unresponsive)\b",
                content,
                re.I,
            ):
                notes.extend(quality.failure_notes)
        else:
            notes.append(_NOTES[issue])
    explanation = "\n\n".join(notes)
    if quality.blocking:
        # Do not leave a false "done" or fabricated figure above a disclaimer.
        return explanation
    return f"{content}\n\n{explanation}" if explanation else content
