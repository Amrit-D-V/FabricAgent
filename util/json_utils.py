from __future__ import annotations

from typing import Any


def strip_json_fences(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last > first:
        text = text[first : last + 1]
    return text


def fix_pbir_node(node: Any) -> None:
    if isinstance(node, dict):
        node.pop("active", None)
        for child in node.values():
            fix_pbir_node(child)
    elif isinstance(node, list):
        for child in node:
            fix_pbir_node(child)
