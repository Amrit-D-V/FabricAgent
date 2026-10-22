from __future__ import annotations

import logging
import re
import sys

from config import CONSOLE_COLOR, CONSOLE_WIDTH

_COLOR_KEYS = ("reset", "dim", "bold", "cyan", "green", "yellow", "red", "blue", "magenta")
_C = {
    "reset": "\033[0m",
    "dim": "\033[2m",
    "bold": "\033[1m",
    "cyan": "\033[36m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
} if CONSOLE_COLOR else dict.fromkeys(_COLOR_KEYS, "")

_UNICODE_OK = False


def enable_utf8_stdout() -> bool:
    global _UNICODE_OK
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            return False
    try:
        "─│→".encode(sys.stdout.encoding or "ascii")
    except Exception:
        return False
    _UNICODE_OK = True
    return True


def _g(fancy: str, plain: str) -> str:
    return fancy if _UNICODE_OK else plain


_RX_TOOL_START = re.compile(
    r"^\[assistant_sdk]\s+-> tool (\S+)(?:\s+args=(.*))?$", re.DOTALL
)
_RX_TOOL_DONE = re.compile(r"^\[assistant_sdk]\s+<- tool (\S+) done in ([\d.]+)s$")
_RX_TEXT = re.compile(r"^\[assistant_sdk]\s+text\((\d+) chars\):\s*(.*)$", re.DOTALL)
_RX_THINKING = re.compile(
    r"^\[assistant_sdk] \.\.\. model thinking/generating \(\+(\d+)s\)$"
)
_RX_RATE = re.compile(r"^\[assistant_sdk] rate-limited \(\+(\d+)s\)$")
_RX_QUERY = re.compile(
    r"^\[assistant_sdk] query sent: prompt_chars=(\d+) \(reuse=(\w+)\)$"
)
_RX_RESULT = re.compile(
    r"^Power BI Assistant \[(\w+)]: (\d+)->(\d+) tokens, duration=(\d+)ms is_error=(\w+)"
)
_RX_VERIFY = re.compile(r"^\[assistant_sdk] verify round (\d+): (.*)$")


def _rule(label: str, colour: str) -> str:
    line = _g("─", "-")
    head = f"{line}{line} {label} "
    pad = max(0, CONSOLE_WIDTH - len(head))
    return f"{_C[colour]}{_C['bold']}{head}{line * pad}{_C['reset']}"


def _fmt_duration(ms: float) -> str:
    s = ms / 1000.0
    return f"{s:.1f}s" if s < 60 else f"{int(s // 60)}m{int(s % 60):02d}s"


def _clip(text: str, limit: int) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + _g("…", "...")


def render(message: str) -> str | None:
    arrow_r = _g("→", "->")
    arrow_l = _g("←", "<-")
    dot = _g("•", "*")

    m = _RX_QUERY.match(message)
    if m:
        chars, reuse = m.group(1), m.group(2)
        return (
            _rule("PROMPT SENT", "cyan")
            + f"\n  {_C['dim']}{chars} chars | reuse={reuse}{_C['reset']}"
        )

    m = _RX_TOOL_START.match(message)
    if m:
        name, args = m.group(1), m.group(2) or ""
        row = f"  {_C['blue']}{arrow_r}{_C['reset']} {_C['bold']}{name}{_C['reset']}"
        if args.strip():
            row += f"\n      {_C['dim']}{_clip(args, CONSOLE_WIDTH - 10)}{_C['reset']}"
        return row

    m = _RX_TOOL_DONE.match(message)
    if m:
        name, secs = m.group(1), float(m.group(2))
        colour = "yellow" if secs >= 5 else "green"
        return (
            f"  {_C['green']}{arrow_l}{_C['reset']} {_C['dim']}{name}{_C['reset']} "
            f"{_C[colour]}{secs:.1f}s{_C['reset']}"
        )

    m = _RX_TEXT.match(message)
    if m:
        chars, body = m.group(1), m.group(2)
        body = body.strip().strip("\"'")
        return (
            f"  {_C['magenta']}{dot}{_C['reset']} {_clip(body, CONSOLE_WIDTH - 12)} "
            f"{_C['dim']}({chars}c){_C['reset']}"
        )

    m = _RX_THINKING.match(message)
    if m:
        return f"  {_C['dim']}... thinking  +{m.group(1)}s{_C['reset']}"

    m = _RX_RATE.match(message)
    if m:
        return f"  {_C['yellow']}!! rate-limited  +{m.group(1)}s{_C['reset']}"

    m = _RX_VERIFY.match(message)
    if m:
        return (
            f"  {_C['cyan']}{dot} verify round {m.group(1)}: {m.group(2)}{_C['reset']}"
        )

    m = _RX_RESULT.match(message)
    if m:
        provider, tin, tout, ms, err = m.groups()
        ok = err.lower() == "false"
        status = (
            f"{_C['green']}ok{_C['reset']}" if ok else f"{_C['red']}ERROR{_C['reset']}"
        )
        return (
            _rule("TURN COMPLETE", "green" if ok else "red")
            + f"\n  {provider} | {tin}{arrow_r}{tout} tokens | "
            f"{_fmt_duration(float(ms))} | {status}"
        )
    return None


def prompt_block(prompt: str, limit: int) -> str:
    bar = _g("│", "|")
    body = (prompt or "").strip()
    truncated = len(body) > limit
    if truncated:
        body = body[:limit]
    lines = [f"  {_C['dim']}{bar}{_C['reset']} {ln}" for ln in body.splitlines()]
    if truncated:
        lines.append(
            f"  {_C['dim']}{bar} ... [{len(prompt) - limit} more chars; full prompt in "
            f"logs/app.log when LOG_FULL_PROMPT=1]{_C['reset']}"
        )
    return "\n".join(lines)


class DashboardFormatter(logging.Formatter):

    def __init__(self) -> None:
        super().__init__(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S"
        )

    def format(self, record: logging.LogRecord) -> str:
        try:
            pretty = render(record.getMessage())
            if pretty is not None:
                return pretty
        except Exception:
            pass  # presentation must never break logging
        return super().format(record)
