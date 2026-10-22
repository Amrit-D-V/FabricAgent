"""Session and OAuth-state cookies, as pure values.

Under Streamlit these had to be written by JavaScript inside a components
iframe, which meant they could never be ``HttpOnly`` and a sign-out could not
reliably delete them (hence the old ``_LOGGED_OUT`` flag). Served from a real
HTTP API they are ordinary ``Set-Cookie`` headers, so this module only encodes
and decodes; :mod:`api.cookies` attaches them to responses.
"""

from __future__ import annotations

import json

from auth import crypto_store

COOKIE_NAME = "fabricbi_session"
TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days, sliding — refreshed on every valid read

# The OAuth nonce still cannot live in request state: signing in navigates the
# browser to Entra and back, and the return trip is a new request.
STATE_COOKIE_NAME = "fabricbi_oauth_state"
STATE_TTL_SECONDS = 10 * 60


def serialize_session(email: str, role: str, name: str = "") -> str:
    """The encrypted cookie value identifying a signed-in user."""
    return crypto_store.encrypt(
        json.dumps({"email": email, "role": role, "name": name or email})
    )


def parse_session(token: str) -> dict | None:
    """Decode a session cookie, or None if absent/expired/tampered."""
    if not token:
        return None
    try:
        return json.loads(crypto_store.decrypt(token, ttl=TTL_SECONDS))
    except Exception:
        return None


def serialize_state(nonce: str) -> str:
    return crypto_store.encrypt(nonce)


def parse_state(token: str) -> str:
    """The nonce this browser was issued, or "" if absent/expired/tampered."""
    if not token:
        return ""
    try:
        return crypto_store.decrypt(token, ttl=STATE_TTL_SECONDS)
    except Exception:
        return ""
