from __future__ import annotations

import msal

from core.context import state

from config import (
    ENTRA_REDIRECT_URI,
    ENTRA_SSO_CLIENT_ID,
    ENTRA_SSO_CLIENT_SECRET,
    ENTRA_SSO_TENANT_ID,
    FABRIC_ITEMS_SCOPE,
    PBI_TENANT_SCOPES,
    PBI_WORKSPACE_SCOPE,
)

_AUTHORITY = f"https://login.microsoftonline.com/{ENTRA_SSO_TENANT_ID}"

# Login only asks for identity. The Power BI token is fetched separately with
# the refresh token: MSAL cannot mix resources in one request, and asking for
# Power BI up front would put a consent prompt in front of every sign-in.
SCOPES = ["User.Read"]
_CACHE_KEY = "_entra_token_cache"


def _load_cache(email: str = "") -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    blob = ""
    try:
        blob = state().get(_CACHE_KEY) or ""
    except Exception:
        blob = ""
    if not blob and email:
        from db.database import load_user_token_cache

        blob = load_user_token_cache(email)
    if blob:
        try:
            cache.deserialize(blob)
        except Exception:
            pass
    return cache


def _save_cache(cache: msal.SerializableTokenCache, email: str = "") -> None:
    if not cache.has_state_changed:
        return
    blob = cache.serialize()
    try:
        state()[_CACHE_KEY] = blob
    except Exception:
        pass
    if email:
        from db.database import save_user_token_cache

        try:
            save_user_token_cache(email, blob)
        except Exception:
            pass


def _app(cache: msal.SerializableTokenCache) -> msal.ConfidentialClientApplication:
    return msal.ConfidentialClientApplication(
        ENTRA_SSO_CLIENT_ID,
        authority=_AUTHORITY,
        client_credential=ENTRA_SSO_CLIENT_SECRET,
        token_cache=cache,
    )


def configured() -> bool:
    return bool(ENTRA_SSO_CLIENT_ID and ENTRA_SSO_TENANT_ID and ENTRA_SSO_CLIENT_SECRET)


def auth_url(state: str = "login") -> str:
    return _app(_load_cache()).get_authorization_request_url(
        SCOPES,
        redirect_uri=ENTRA_REDIRECT_URI,
        state=state,
        prompt="select_account",
    )


def exchange_code(code: str) -> dict:
    cache = _load_cache()
    result = (
        _app(cache).acquire_token_by_authorization_code(
            code, scopes=SCOPES, redirect_uri=ENTRA_REDIRECT_URI
        )
        or {}
    )
    email = (
        ((result.get("id_token_claims") or {}).get("preferred_username") or "")
        .lower()
        .strip()
    )
    _save_cache(cache, email)
    return result


# Returns (token, error) from the stored refresh token. An empty token is
# always a failure -- never read it as "this user has no access".
def _acquire(email: str, scope: str) -> tuple[str, str]:
    if not configured():
        return "", "Microsoft sign-in is not configured."
    email = (email or "").lower().strip()
    cache = _load_cache(email)
    app = _app(cache)

    account = None
    for acc in app.get_accounts(username=email) or app.get_accounts():
        account = acc
        break
    if account is None:
        return "", "no cached Microsoft account — sign in again"

    result = app.acquire_token_silent([scope], account=account) or {}
    _save_cache(cache, email)
    token = result.get("access_token") or ""
    if token:
        return token, ""
    err = (
        result.get("error_description")
        or result.get("error")
        or "could not obtain a Power BI token for this user"
    )
    return "", str(err)[:300]


def acquire_powerbi_token(email: str) -> tuple[str, str]:
    return _acquire(email, PBI_WORKSPACE_SCOPE)


# The caller's own admin token. Entra issues it to anyone, but it only opens
# the admin endpoints for a real Fabric administrator -- the tenant role rides
# in the token, so this cannot be used to impersonate one.
def acquire_admin_token(email: str) -> tuple[str, str]:
    err = ""
    for scope in PBI_TENANT_SCOPES:
        token, err = _acquire(email, scope)
        if token:
            return token, ""
    return "", err


def acquire_fabric_token(email: str) -> tuple[str, str]:
    return _acquire(email, FABRIC_ITEMS_SCOPE)


def sign_out(email: str = "") -> None:
    try:
        state().pop(_CACHE_KEY, None)
    except Exception:
        pass
    if email:
        from db.database import clear_user_token_cache, clear_user_workspace_access

        try:
            clear_user_token_cache(email)
            clear_user_workspace_access(email)
        except Exception:
            pass
