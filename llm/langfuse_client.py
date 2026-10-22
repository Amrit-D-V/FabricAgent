from __future__ import annotations

import logging
import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from config import LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY

logger = logging.getLogger(__name__)

_client: Any = None
_init_attempted = False


def get_langfuse() -> Any | None:
    global _client, _init_attempted
    if _init_attempted:
        return _client
    _init_attempted = True
    if not LANGFUSE_PUBLIC_KEY or not LANGFUSE_SECRET_KEY:
        return None
    try:
        from langfuse import Langfuse

        _client = Langfuse(
            public_key=LANGFUSE_PUBLIC_KEY,
            secret_key=LANGFUSE_SECRET_KEY,
            host=LANGFUSE_HOST,
        )
        logger.info("Langfuse client initialised at %s", LANGFUSE_HOST)
    except ImportError:
        logger.warning(
            "langfuse package not installed — tracing disabled. pip install langfuse"
        )
    except Exception as exc:
        logger.warning("Langfuse init failed: %s", exc)
    return _client


class _TurnTracer:

    def __init__(self, trace: Any | None) -> None:
        self._trace = trace
        self._t0 = time.perf_counter()

    def span(
        self,
        name: str,
        input: dict | None = None,
        output: Any = None,
        error: str | None = None,
    ) -> None:
        if self._trace is None:
            return
        try:
            kwargs: dict[str, Any] = {"name": name}
            if input is not None:
                kwargs["input"] = input
            if output is not None:
                kwargs["output"] = output
            span = self._trace.span(**kwargs)
            if error:
                span.end(level="ERROR", status_message=error)
            else:
                span.end()
        except Exception as exc:
            logger.debug("Langfuse span error: %s", exc)

    def score(self, name: str, value: float, comment: str = "") -> None:
        if self._trace is None:
            return
        try:
            self._trace.score(name=name, value=value, comment=comment)
        except Exception as exc:
            logger.debug("Langfuse score error: %s", exc)

    def complete(self, events: list[dict]) -> None:
        if self._trace is None:
            return
        try:
            tool_calls = [e for e in events if e.get("type") == "tool_call"]
            text_out = " ".join(
                e.get("content", "") for e in events if e.get("type") == "text"
            )
            elapsed = time.perf_counter() - self._t0
            for tc in tool_calls:
                self.span(
                    name=tc.get("tool", "tool_call"),
                    input=tc.get("input"),
                    output=tc.get("result", "")[:1000] if tc.get("result") else None,
                    error=tc.get("error"),
                )
            self._trace.update(
                output=text_out[:2000] if text_out else None,
                metadata={
                    "tool_call_count": len(tool_calls),
                    "elapsed_s": round(elapsed, 2),
                },
            )
        except Exception as exc:
            logger.debug("Langfuse trace complete error: %s", exc)


@contextmanager
def trace_turn(
    lf: Any | None,
    user_message: str,
    session_id: str = "",
    model: str = "",
) -> Generator[_TurnTracer, None, None]:
    trace = None
    if lf is not None:
        try:
            trace = lf.trace(
                name="assistant_turn",
                input=user_message[:2000],
                session_id=session_id or None,
                metadata={"model": model} if model else {},
            )
        except Exception as exc:
            logger.debug("Langfuse trace start error: %s", exc)
    tracer = _TurnTracer(trace)
    try:
        yield tracer
    finally:
        try:
            if lf is not None:
                lf.flush()
        except Exception:
            pass
