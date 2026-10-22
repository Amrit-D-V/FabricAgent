"""Attaching the session and OAuth-state cookies to responses.

These are real Set-Cookie headers, so unlike the Streamlit version they can be
HttpOnly (JavaScript never needs to read them -- /api/session answers instead)
and deleting one actually reaches the browser.
"""

from __future__ import annotations

from starlette.responses import Response

from auth.session_cookie import (
    COOKIE_NAME,
    STATE_COOKIE_NAME,
    STATE_TTL_SECONDS,
    TTL_SECONDS,
    serialize_session,
    serialize_state,
)
from config import SESSION_COOKIE_SECURE

_COMMON = {"httponly": True, "samesite": "lax", "path": "/", "secure": SESSION_COOKIE_SECURE}


def set_session(response: Response, email: str, role: str, name: str = "") -> Response:
    response.set_cookie(
        COOKIE_NAME, serialize_session(email, role, name), max_age=TTL_SECONDS, **_COMMON
    )
    return response


def clear_session(response: Response) -> Response:
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


def set_state(response: Response, nonce: str) -> Response:
    response.set_cookie(
        STATE_COOKIE_NAME, serialize_state(nonce), max_age=STATE_TTL_SECONDS, **_COMMON
    )
    return response


def clear_state(response: Response) -> Response:
    response.delete_cookie(STATE_COOKIE_NAME, path="/")
    return response
