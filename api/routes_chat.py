"""The assistant endpoint, and the per-project transcript behind it.

Chat history is stored per project *and* per user (chat_history_v2), so two
people on the same project keep separate threads. A conversation with no
project selected is not persisted -- there is nothing to key it on -- which
matches what the Streamlit page did.
"""

from __future__ import annotations

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

from api.chat import health_info, run_chat
from api.registry import json_body, route
from auth.auth import visible_projects
from core.context import current_email, state
from core.errors import AccessDenied, AppError
from db import audit_context


def _project_id() -> int | None:
    """The active project, re-checked against the caller's own listing.

    None means "no project selected", which is allowed -- the thread just is
    not saved. An id the caller cannot see is a refusal, not a silent None.
    """
    pid = state().get("active_project_id")
    if not pid:
        return None
    if not any(p.id == pid for p in visible_projects()):
        raise AccessDenied("You do not have access to that project.")
    return pid


def _normalize(message: dict) -> dict:
    """One message shape for the UI, old transcripts included."""
    if message.get("content") is not None or "events" not in message:
        return {
            "role": message.get("role", "assistant"),
            "content": message.get("content") or "",
            "steps": message.get("steps") or [],
            "visuals": message.get("visuals") or [],
        }

    texts: list[str] = []
    steps: list[str] = []
    for event in message.get("events") or []:
        if not isinstance(event, dict):
            continue
        if event.get("type") == "text":
            body = (event.get("content") or "").strip()
            if body:
                texts.append(body)
        elif event.get("type") == "tool_call":
            steps.append(f"Called {event.get('name') or 'a tool'}")

    return {
        "role": message.get("role", "assistant"),
        "content": texts[-1] if texts else "",
        "steps": texts[:-1] + steps,
        "visuals": [],
    }


@route("/chat/history", project=False)
def history(_request: Request):
    """The saved thread for this project and user."""
    from db.database import load_chat_state

    pid = _project_id()
    if not pid:
        return {"messages": [], "persisted": False}
    messages, _claude_history = load_chat_state(pid, current_email())
    return {"messages": [_normalize(m) for m in messages], "persisted": True}


@route("/chat/history", methods=("DELETE",), project=False)
def clear_history(_request: Request):
    from db.database import clear_chat_state

    pid = _project_id()
    if not pid:
        return {"ok": True}
    clear_chat_state(pid, current_email())
    return {"ok": True}


@route("/chat", methods=("POST",), project=False)
async def chat(request: Request):
    body = await json_body(request)
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise AppError("Expected a non-empty 'messages' array.")

    state()["messages"] = messages

    pid = _project_id()
    email = current_email()
    snapshot = audit_context.get_context()

    if body.get("stream") is True:
        from api.sse import stream as sse_stream

        def stream_work(emit):
            audit_context.adopt_context(snapshot)
            emit("progress", {"message": "Thinking..."})
            reply = run_chat(messages, body.get("mode") or "dashboard")
            if pid:
                from db.database import save_chat_state
                thread = [
                    *messages,
                    {
                        "role": "assistant",
                        "content": reply["content"],
                        "steps": reply.get("steps", []),
                        "visuals": reply.get("visuals", []),
                        "quality": reply.get("quality", {}),
                    },
                ]
                try:
                    save_chat_state(pid, thread, [], user_email=email)
                except Exception:
                    pass
            return reply

        return sse_stream(stream_work)

    def call():
        audit_context.adopt_context(snapshot)
        reply = run_chat(messages, body.get("mode") or "dashboard")

        if pid:
            from db.database import save_chat_state

            thread = [
                *messages,
                {
                    "role": "assistant",
                    "content": reply["content"],
                    "steps": reply.get("steps", []),
                    "visuals": reply.get("visuals", []),
                    "quality": reply.get("quality", {}),
                },
            ]
            try:
                save_chat_state(pid, thread, [], user_email=email)
            except Exception:
                pass
        return reply

    return await run_in_threadpool(call)


@route("/health", public=True)
def health(_request: Request):
    return health_info()
