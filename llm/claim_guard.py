from __future__ import annotations

import re

from model.model_edit_guard import classify_modeling_op
from util.tool_result import is_failure

_PAST = (
    r"(?:created|built|rebuilt|recreated|added|applied|updated|deleted|removed"
    r"|renamed|wired|placed|injected|generated|styled|saved|fixed|repaired"
    r"|resolved|rebuilt)"
)
_PROGRESSIVE = (
    r"(?:creating|building|rebuilding|recreating|adding|applying|updating"
    r"|deleting|removing|renaming|wiring|placing|injecting|generating|styling)"
)

_CLAIM_PATTERNS = [
    re.compile(
        rf"\bI(?:'ve| have| had)?(?:\s+(?:already|now|just|successfully))?\s+{_PAST}\b",
        re.IGNORECASE,
    ),
    re.compile(rf"\b(?:has|have) been {_PAST}\b", re.IGNORECASE),
    re.compile(rf"\bsuccessfully {_PAST}\b", re.IGNORECASE),
    re.compile(rf"(?m)^\s*(?:[-*•]|\d+[.)])\s*{_PAST}\b", re.IGNORECASE),
    re.compile(
        rf"\bI(?:'m| am)(?:\s+(?:actively|now|currently))?\s+{_PROGRESSIVE}\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:build|rebuild|report|page|update|fix)\s+is\s+(?:now\s+)?"
        r"(?:complete|done|finished)\b",
        re.IGNORECASE,
    ),
]

_FENCED_CODE = re.compile(r"```.*?```", re.DOTALL)

WARNING_NO_WRITES = (
    "⚠️ **Reliability check:** the reply above describes changes, but "
    "**no write tool ran this turn** — nothing was actually created or modified. "
    "Treat those claims as NOT done. (The model narrated work instead of calling "
    "tools; Claude is recommended for build turns.)"
)

WARNING_ALL_FAILED = (
    "⚠️ **Reliability check:** every write tool call this turn "
    "**failed or was denied**, so the changes described above did **not** "
    "happen. See the tool results for the actual errors."
)


def claims_change(text: str) -> bool:
    stripped = _FENCED_CODE.sub("", text or "")
    return any(p.search(stripped) for p in _CLAIM_PATTERNS)


def _is_write_tool(name: str, args: dict) -> bool:
    kind = classify_modeling_op(name, args or {})
    if kind == "read":
        return False
    if kind == "mutate":
        return True
    return True


def ground_events(events: list[dict], readonly_tools=frozenset()) -> list[dict]:
    try:
        text = "\n".join(
            str(e.get("content") or "") for e in events if e.get("type") == "text"
        )
        if not text or not claims_change(text):
            return events
        attempted = succeeded = False
        for ev in events:
            if ev.get("type") != "tool_call":
                continue
            name = str(ev.get("name") or "")
            if name in readonly_tools:
                continue
            if not _is_write_tool(name, ev.get("input") or {}):
                continue
            attempted = True
            # Was `result.startswith(("ERROR","DENIED","✗"))`, which matched
            # almost no real failure: write tools return "[format] ERROR — …",
            # "[cond-color] SKIP — …", a lowercase "Error: pbi command timed
            # out", or "Desktop Bridge unavailable: …". Every one of those was
            # read as a success, so this warning stayed silent in exactly the
            # turns it exists to catch.
            if not is_failure(ev.get("result")):
                succeeded = True
                break
        if succeeded:
            return events
        events.append(
            {
                "type": "text",
                "content": WARNING_ALL_FAILED if attempted else WARNING_NO_WRITES,
            }
        )
    except Exception:
        pass
    return events
