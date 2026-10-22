"""Tool evidence from provider messages, separate from generated prose.

Claude's complete assistant/user messages accompany the partial text stream.
Only a matching tool_result confirms completion; a tool_use alone does not.
See https://code.claude.com/docs/en/agent-sdk/typescript.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from util.tool_result import is_failure


def bare_tool_name(name: str) -> str:
    return name.split("__", 2)[-1] if name.startswith("mcp__") else name


@dataclass(frozen=True)
class ToolEvidence:
    call_id: str
    name: str
    input: dict = field(default_factory=dict)
    result: object = None
    is_error: bool = False
    completed: bool = True

    @property
    def succeeded(self) -> bool:
        return self.completed and not self.is_error and not is_failure(self.result)


class ClaudeToolCollector:
    """Pair complete messages by call ID; tolerate replays and missing results."""

    def __init__(self):
        self._calls: dict[str, dict] = {}
        self._results: dict[str, dict] = {}
        self._emitted: set[str] = set()

    def feed(self, message: dict) -> list[ToolEvidence]:
        if message.get("type") not in ("assistant", "user"):
            return []
        body = message.get("message") or {}
        blocks = body.get("content") if isinstance(body, dict) else None
        if not isinstance(blocks, list):
            return []
        evidence = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if message["type"] == "assistant" and block.get("type") == "tool_use":
                call_id = block.get("id")
                if isinstance(call_id, str) and call_id:
                    self._calls[call_id] = block
            elif message["type"] == "user" and block.get("type") == "tool_result":
                call_id = block.get("tool_use_id")
                if isinstance(call_id, str) and call_id:
                    self._results[call_id] = block
            else:
                continue
            if not isinstance(call_id, str) or not call_id:
                continue
            if call_id in self._calls and call_id in self._results:
                if call_id not in self._emitted:
                    evidence.append(self._evidence(call_id))
                    self._emitted.add(call_id)
        return evidence

    def _evidence(self, call_id: str) -> ToolEvidence:
        call = self._calls.get(call_id, {})
        result = self._results.get(call_id, {})
        args = call.get("input") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        if isinstance(args, dict) and set(args) == {"kwargs"}:
            args = args["kwargs"]
        return ToolEvidence(
            call_id=call_id,
            name=bare_tool_name(str(call.get("name") or "unknown")),
            input=args if isinstance(args, dict) else {},
            result=result.get("content"),
            is_error=bool(result.get("is_error")),
            completed=call_id in self._calls and call_id in self._results,
        )

    def finish(self) -> list[ToolEvidence]:
        remaining = (self._calls.keys() | self._results.keys()) - self._emitted
        evidence = [self._evidence(key) for key in sorted(remaining)]
        self._emitted.update(remaining)
        return evidence
