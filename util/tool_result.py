from __future__ import annotations

import json
import re

# Leading `[some-tag] ` that several report writers prepend to both their
# success and failure strings.
_TAG_PREFIX = re.compile(r"^\[[^\]\n]{1,40}\]\s*")

# Matched case-insensitively against the start of the message body, after any
# tag prefix is removed.
_FAILURE_PREFIXES = (
    "error",
    "denied",
    "refused",
    "failed",
    "fatal",
    # SKIP is a real failure for the caller's purpose: the tool declined to act
    # (usually a wrong visual type), so whatever was requested did not happen.
    "skip",
    "invalid:",
    "✗",
    "⚠",
)

# Failures that begin with neither a tag nor one of the words above. Kept
# deliberately short and specific — a loose substring here would start
# reporting successful writes as failures, which is the worse error.
_FAILURE_PHRASES = (
    "timed out",
    "bridge unavailable",
    "is unavailable",
    "no such option",
    "traceback (most recent call last)",
    "nothing may have been written",
)


_STATUS_FAILURES = frozenset({
    "error",
    "failed",
    "denied",
    "refused",
    "invalid",
    "skipped",
})


def is_failure(result: object) -> bool:
    if result is None:
        return True
    if isinstance(result, bool):
        return not result
    if isinstance(result, dict):
        return _structured_failure(result)
    if isinstance(result, list):
        if not result:
            return True
        blocks = [b for b in result if isinstance(b, dict) and b.get("type") == "text"]
        return any(is_failure(b.get("text")) for b in blocks)

    text = str(result).strip()
    if not text:
        return True

    if text[0] in "{[":
        try:
            return is_failure(json.loads(text))
        except (ValueError, TypeError):
            pass

    body = _TAG_PREFIX.sub("", text).lstrip()
    low = body.lower()

    if low.startswith(_FAILURE_PREFIXES):
        return True

    return any(phrase in low[:200] for phrase in _FAILURE_PHRASES)


def _structured_failure(result: dict) -> bool:
    if not result:
        return True
    if result.get("isError") is True or result.get("is_error") is True:
        return True
    if any(result.get(key) is False for key in ("success", "ok", "valid")):
        return True
    if str(result.get("status", "")).lower() in _STATUS_FAILURES:
        return True
    if result.get("error") or result.get("errors"):
        return True
    if result.get("type") == "text":
        return is_failure(result.get("text"))
    if isinstance(result.get("content"), list):
        return is_failure(result["content"])
    return False


def is_success(result: object) -> bool:
    return not is_failure(result)
