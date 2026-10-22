"""Server-sent events for the long-running jobs.

Wireframe generation and model building take minutes and report progress
through callbacks. Streamlit rendered those callbacks straight into the page;
over HTTP they become an event stream, so the browser sees the same "extracting
requirements… generating wireframe 2 of 3…" trail instead of a frozen spinner.

One helper, because both pipelines have the same shape: run blocking work in a
thread, push progress events, finish with a `done` (or `error`) event.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

from starlette.responses import StreamingResponse

from db import audit_context

logger = logging.getLogger("fabricbi.api")

_DONE = object()


def stream(work: Callable[[Callable[[str, Any], None]], Any]) -> StreamingResponse:
    """Run `work` in a thread, streaming whatever it emits.

    `work` is handed an ``emit(event, data)`` callable it may call from its own
    thread as often as it likes. Its return value is sent as the final `done`
    event.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    # The worker runs off the event loop, so audit_context (thread-local) has
    # to be carried over the same way api.registry does it.
    snapshot = audit_context.get_context()

    def emit(event: str, data: Any = None) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (event, data))

    def run() -> None:
        audit_context.adopt_context(snapshot)
        try:
            result = work(emit)
            emit("done", result)
        except Exception as exc:  # a failed job is a stream event, not a 500
            logger.exception("streamed job failed")
            emit("error", {"message": str(exc)})
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, _DONE)

    async def events():
        task = asyncio.get_running_loop().run_in_executor(None, run)
        try:
            while True:
                item = await queue.get()
                if item is _DONE:
                    break
                event, data = item
                yield f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
        finally:
            await task

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
    )
