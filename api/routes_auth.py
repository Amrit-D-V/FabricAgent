"""Sign-in, sign-out, and "who am I".

The browser round trip that Streamlit drove with query params and reruns is
three plain endpoints now: /login/start sends the user to Microsoft,
/login/callback is where Entra returns, and /session answers who they are.
"""

from __future__ import annotations

import os

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from api import cookies
from api.registry import route
from auth import auth
from auth.session_cookie import STATE_COOKIE_NAME
from core.context import current_user

# Where to drop the user after the Entra round trip. The API and the UI are
# different origins in development, so this cannot just be "/".
UI_ORIGIN = os.getenv("FABRICBI_UI_ORIGIN", "http://localhost:5173").rstrip("/")


@route("/session", public=True)
def session(_request: Request):
    """The signed-in user, or null. Drives the whole shell, so it is public:
    'nobody is signed in' is a legitimate answer, not an error."""
    user = current_user()
    return {"user": user, "ssoConfigured": auth.sso_configured()}


@route("/login/start", public=True)
def login_start(_request: Request):
    """Hand the browser a Microsoft URL and remember the nonce that pins it."""
    if not auth.sso_configured():
        return JSONResponse(
            {
                "error": "Microsoft sign-in isn't configured -- set AZURE_CLIENT_ID, "
                "AZURE_TENANT_ID and AZURE_CLIENT_SECRET in .env."
            },
            status_code=503,
        )
    url, nonce = auth.begin_login()
    return cookies.set_state(JSONResponse({"url": url}), nonce)


@route("/login/callback", public=True)
def login_callback(request: Request):
    """Where Entra returns. Redirects back into the UI either way, because a
    browser lands here directly and cannot render a JSON error."""
    from core.errors import AppError

    code = request.query_params.get("code", "")
    returned = request.query_params.get("state", "")
    expected = _expected_state(request)

    try:
        user = auth.complete_login(code, returned, expected, ip=_client_ip(request))
    except AppError as exc:
        response = RedirectResponse(f"{UI_ORIGIN}/signin?error={_quote(str(exc))}")
        return cookies.clear_state(response)

    response = RedirectResponse(UI_ORIGIN + "/")
    cookies.clear_state(response)
    return cookies.set_session(response, user["email"], user["role"], user["name"])


@route("/logout", methods=("POST",), project=False)
def logout(_request: Request):
    user = current_user() or {}
    auth.logout(user.get("email", ""))
    return cookies.clear_session(JSONResponse({"ok": True}))


def _expected_state(request: Request) -> str:
    from auth.session_cookie import parse_state

    return parse_state(request.cookies.get(STATE_COOKIE_NAME, ""))


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def _quote(text: str) -> str:
    from urllib.parse import quote

    return quote(text[:300])
