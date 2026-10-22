from __future__ import annotations

import re
from collections.abc import Callable

from db.database import save_api_usage
from llm.llm_client import UsageInfo, stream_text

_THINK = re.compile(r"<think>.*?</think>", flags=re.DOTALL)


def _collect(
    stream,
    progress_callback: Callable[[str], None] | None = None,
    progress_label: str = "output",
    tick_every: int = 500,
) -> tuple[str, UsageInfo | None]:
    parts: list[str] = []
    usage: UsageInfo | None = None
    total = 0
    last_tick = 0
    for item in stream:
        if isinstance(item, UsageInfo):
            usage = item
        else:
            parts.append(item)
            if progress_callback:
                total += len(item)
                if total - last_tick >= tick_every:
                    last_tick = total
                    progress_callback(
                        f"LLM streaming {progress_label}... {total:,} chars received"
                    )
    raw = _THINK.sub("", "".join(parts)).strip()
    return raw, usage


def stream_collect(
    system: str,
    user: str,
    *,
    agent: str,
    max_tokens: int,
    temperature: float | None = None,
    progress_callback: Callable[[str], None] | None = None,
    progress_label: str = "output",
    tick_every: int = 500,
) -> tuple[str, UsageInfo | None]:
    return _collect(
        stream_text(
            system,
            user,
            max_tokens=max_tokens,
            agent=agent,
            temperature=temperature,
        ),
        progress_callback,
        progress_label,
        tick_every,
    )


def log_usage(
    project_id: int | None,
    agent: str,
    usage: UsageInfo | None,
    latency_ms: int,
    status: str = "success",
) -> None:
    if project_id and usage:
        save_api_usage(
            project_id=project_id,
            agent=agent,
            model=usage.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            latency_ms=latency_ms,
            status=status,
        )


if __name__ == "__main__":
    raw, usage = _collect(["<think>reason</think>", "{", '"a":1', "}"])
    assert raw == '{"a":1}', raw
    assert usage is None

    msgs: list[str] = []
    _collect(
        ["x" * 300, "y" * 300],
        progress_callback=msgs.append,
        progress_label="plan JSON",
        tick_every=500,
    )
    assert msgs == ["LLM streaming plan JSON... 600 chars received"], msgs

    quiet: list[str] = []
    _collect(["x" * 100], progress_callback=quiet.append, tick_every=500)
    assert quiet == [], quiet

    print("LLM_HELPER_OK")
