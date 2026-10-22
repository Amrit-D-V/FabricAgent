from __future__ import annotations

"""Provider-agnostic pieces of the assistant agent loop.

Extracted verbatim from app.py, which had grown to 4,407 lines holding both
the Streamlit page and the agent orchestration behind it. Everything here is
shared by more than one provider driver, has no Streamlit dependency, and is
therefore testable on its own -- which the originals were not, because
importing app.py runs st.set_page_config() and require_login().

app.py re-imports these names so existing call sites keep working; see the
same backward-compatible re-export pattern used for fabric.workspace_context
and report.scan_cache.
"""

from util.tool_result import is_failure

_READONLY_TOOLS = {
    "render_chart",
    "render_map",
    "pbi_paginated_list",
    "pbi_paginated_get",
    "pbi_paginated_validate",
    "pbi_report_info",
    "pbi_report_validate",
    "pbi_report_health_check",
    "pbi_visual_list",
    "pbi_model_quality",
    "pbi_model_bpa",
    "pbi_model_test",
    "pbi_generate_docs",
    "pbi_export_model",
    "pbi_dax_page",
    "pbi_dax_analyze",
    "pbi_visual_bindings",
    "pbi_reports_list",
    "pbi_memory_search",
    "pbi_impact_analysis",
    "pbi_lineage_map",
    "pbi_workspace_items",
    "pbi_design_judge",
    "pbi_layout_compute",
    "pbi_dashboard_layout",
    "pbi_dashboard_audit",
    "pbi_refresh_registry",
    "pbi_report_review",
}


def _ground_agent_events(events: list[dict]) -> list[dict]:
    from llm.claim_guard import ground_events

    return ground_events(events, _READONLY_TOOLS)


def _mark_rolling_cache(messages: list[dict]) -> None:
    if not messages:
        return
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict):
                    b.pop("cache_control", None)
    last = messages[-1]
    c = last.get("content")
    if isinstance(c, str):
        last["content"] = [
            {"type": "text", "text": c, "cache_control": {"type": "ephemeral"}}
        ]
    elif isinstance(c, list) and c and isinstance(c[-1], dict):
        c[-1]["cache_control"] = {"type": "ephemeral"}


def _verify_agent_output(user_prompt: str, evts: list[dict]) -> tuple[bool, str]:
    if not evts:
        return False, "No events or text output generated."

    full_text = " ".join(e.get("content", "") for e in evts if e.get("type") == "text")
    lower_text = full_text.lower()
    lower_prompt = (user_prompt or "").lower()

    if "[tool_code]" in full_text or "[/tool_code]" in full_text:
        return (
            False,
            "Output contains raw [tool_code] pseudo-code blocks instead of executing native tools.",
        )

    for e in evts:
        if e.get("type") == "tool_call":
            res = str(e.get("result", ""))
            # Was `startswith("ERROR — tool")`, which only caught the
            # dispatcher's own non-zero-exit message and let every tagged
            # "[format] ERROR", SKIP and timeout through as a pass.
            if is_failure(res):
                return False, f"Tool '{e.get('name')}' failed: {res[:150]}"

    chart_keywords = [
        "chart",
        "heatmap",
        "trend",
        "scatter",
        "bubble",
        "plot",
        "breakdown",
    ]
    prompt_wants_chart = any(kw in lower_prompt for kw in chart_keywords)
    text_claims_chart = any(
        phrase in lower_text
        for phrase in [
            "here is a chart",
            "here is a bubble",
            "here is a heatmap",
            "i will generate a chart",
            "plotted below",
            "chart shown",
        ]
    )
    has_chart_tool = any(
        e.get("type") == "tool_call" and e.get("name") in ("render_chart", "render_map")
        for e in evts
    )

    if (prompt_wants_chart or text_claims_chart) and not has_chart_tool:
        return (
            False,
            "Output promises or mentions a chart/visualization, but no "
            "render_chart tool was executed.",
        )

    return True, "Verified clean"


def _unresolved_verification_event(
    health_unresolved: bool,
    health_rounds: int,
    output_unresolved: bool,
    output_rounds: int,
) -> dict | None:
    """A visible warning when retry rounds ran out while a check was still
    failing. Without this, an exhausted-and-still-broken turn ends the same
    way (a silent `break`) as a genuinely clean one — the user sees only the
    model's own last turn text, with no signal that verification never
    actually confirmed the result."""
    if not (health_unresolved or output_unresolved):
        return None
    parts = []
    if health_unresolved:
        parts.append(f"the automated health check after {health_rounds} fix attempt(s)")
    if output_unresolved:
        parts.append(f"output verification after {output_rounds} attempt(s)")
    return {
        "type": "text",
        "content": (
            "⚠️ Automated verification could not fully confirm this report is "
            f"error-free — {' and '.join(parts)} still found issues. Recommend "
            "a manual check."
        ),
    }


def _tool_error_text(name: str, exc: Exception) -> str:
    return (
        f"ERROR — tool '{name}' failed: {type(exc).__name__}: {exc}. "
        f"Fix the inputs and retry, or try a different approach."
    )


def _record_tool_event(
    events: list, max_result_chars: int, name: str, args: dict, result_text: str
) -> str:
    truncated = result_text[:max_result_chars]
    if len(result_text) > max_result_chars:
        truncated += "\n...[truncated]"
    if name == "pbi_report_create" and "ACTIVE_REPORT_PATH=" in result_text:
        for line in result_text.splitlines():
            if line.startswith("ACTIVE_REPORT_PATH="):
                events.append(
                    {
                        "type": "active_report",
                        "path": line.split("=", 1)[1].strip(),
                    }
                )
    events.append(
        {
            "type": "tool_call",
            "name": name,
            "input": args,
            "result": truncated,
        }
    )
    return truncated
