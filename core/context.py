from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any

from db import audit_context

_STATE: ContextVar[dict[str, Any]] = ContextVar("fabricbi_request_state")


def state() -> dict[str, Any]:
    try:
        return _STATE.get()
    except LookupError:
        fresh: dict[str, Any] = {}
        _STATE.set(fresh)
        return fresh


def begin(user: dict | None = None, ip: str = "") -> Token:
    token = _STATE.set({"user": user} if user else {})
    audit_context.set_actor(user_email=(user or {}).get("email", ""), ip_address=ip)
    return token


def end(token: Token) -> None:
    _STATE.reset(token)


def current_user() -> dict | None:
    return state().get("user")


def current_role() -> str:
    return (current_user() or {}).get("role", "viewer")


def current_email() -> str:
    return (current_user() or {}).get("email", "")


def is_admin() -> bool:
    return current_role() == "admin"
