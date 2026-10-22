"""Who is signed in, and what they may do.

The Streamlit version drew the login screen itself and drove the OAuth round
trip with ``st.query_params`` / ``st.rerun`` / ``st.stop``. React owns the
screen now, so what is left here is the decisions -- is this cookie good, may
this account in, what does an Entra callback mean, and what is this user
allowed to do -- with the HTTP mechanics in :mod:`api.routes_auth`.

The authorization half (:func:`visible_projects`, :func:`effective_role`,
:func:`has_capability`) is unchanged in behaviour; it reads the signed-in user
from :mod:`core.context` instead of ``st.session_state``.
"""

from __future__ import annotations

import secrets

from core.cache import ttl_cache
from core.context import current_role, current_user, state
from core.errors import AccessDenied
from db.audit_context import set_actor
from db.database import audit, get_app_user, init_db

_ROLE_RANK = {"viewer": 0, "editor": 1, "admin": 2}


# --- sign-in ---------------------------------------------------------------


def sso_configured() -> bool:
    from auth import entra_sso

    return entra_sso.configured()


def begin_login() -> tuple[str, str]:
    """(url to send the browser to, nonce to store in the state cookie).

    The nonce pins the callback to the browser that started the sign-in, so a
    code injected from another site cannot be redeemed here.
    """
    from auth import entra_sso

    init_db()
    nonce = secrets.token_urlsafe(24)
    return entra_sso.auth_url(state=nonce), nonce


def complete_login(
    code: str, returned_state: str, expected_state: str, ip: str = ""
) -> dict:
    """Redeem an Entra callback and return the signed-in user.

    Raises AccessDenied with a message meant for the user. Entra proves who
    they are; app_users decides whether they may in -- signing in to the tenant
    is NOT authorisation for FabricBI.
    """
    from auth import entra_sso

    init_db()

    if not expected_state or not secrets.compare_digest(expected_state, returned_state):
        audit(
            "login.failure",
            target="entra",
            severity="warning",
            outcome="failure",
            details="oauth state mismatch",
            ip_address=ip,
        )
        raise AccessDenied(
            "Could not verify that sign-in request. Start again from the "
            "sign-in page. If it keeps failing, check that cookies are allowed."
        )

    result = entra_sso.exchange_code(code)
    if not result.get("access_token"):
        detail = (
            result.get("error_description") or result.get("error") or "Sign-in failed."
        )
        audit(
            "login.failure",
            target="entra",
            severity="warning",
            outcome="failure",
            ip_address=ip,
        )
        raise AccessDenied(f"Microsoft sign-in failed: {str(detail)[:200]}")

    claims = result.get("id_token_claims", {}) or {}
    email = (
        (claims.get("preferred_username") or claims.get("email") or "").lower().strip()
    )
    rec = get_app_user(email)
    if not rec:
        audit(
            "login.denied",
            target=email,
            severity="warning",
            outcome="denied",
            details="no app_users record",
            ip_address=ip,
        )
        raise AccessDenied(
            f"{email or 'That account'} is not registered in FabricBI. "
            "Ask an administrator to add it."
        )
    if not rec.get("is_active", True):
        audit(
            "login.blocked",
            target=email,
            severity="warning",
            outcome="denied",
            details="account disabled",
            ip_address=ip,
        )
        raise AccessDenied("This account has been disabled. Contact an administrator.")

    user = {
        "email": email,
        "role": rec.get("role", "viewer"),
        "name": claims.get("name", email),
    }
    try:
        from auth.workspace_access import refresh as _refresh_ws

        _refresh_ws(email)
    except Exception:
        pass
    audit("login.success", user_email=email, ip_address=ip)
    return user


def user_from_session(payload: dict | None) -> dict | None:
    """Re-check a decoded session cookie against app_users, or None if stale."""
    if not payload:
        return None
    email = payload.get("email", "")
    rec = get_app_user(email)
    if not rec or not rec.get("is_active", True):
        return None
    return {
        "email": email,
        "role": rec.get("role", "viewer"),
        "name": payload.get("name", email),
    }


def logout(email: str = "") -> None:
    """Audit the sign-out and drop the stored Microsoft grant.

    The cookie itself is deleted by the response; there is no longer a
    ``_LOGGED_OUT`` flag, because a real Set-Cookie actually reaches the browser.
    """
    audit("logout", user_email=email)
    try:
        from auth import entra_sso

        entra_sso.sign_out(email)
    except Exception:
        pass


# --- authorization ---------------------------------------------------------


@ttl_cache(30)
def _cached_projects_for_user(user_email: str, is_admin: bool):
    from db.database import list_projects_for_user

    return list_projects_for_user(user_email, is_admin)


def refresh_project_list() -> None:
    _cached_projects_for_user.cache_clear()


def active_project_id() -> int | None:
    """The project the current request is acting on (set by the API middleware)."""
    return state().get("active_project_id")


def visible_projects() -> list:
    """The projects this user may open -- the only project listing a caller may use.

    db.database._list_all_projects answers for everyone at once, so a caller
    that reached for it handed a signed-in user every other client's work.
    Membership and the Fabric gate live in list_projects_for_user; this is the
    way there.

    current_role(), not effective_role(): effective_role is scoped to the
    active project, which is the very thing this list decides.
    """
    user = current_user() or {}
    projects = _cached_projects_for_user(
        user.get("email", ""), current_role() == "admin"
    )

    active = active_project_id()
    if active is not None and not any(p.id == active for p in projects):
        state()["active_project_id"] = None
        state()["active_project_name"] = ""
    return projects


def require_project():
    from core.errors import AccessDenied, NotFound

    pid = active_project_id()
    if not pid:
        raise NotFound("Select a project first.")
    project = next((p for p in visible_projects() if p.id == pid), None)
    if not project:
        raise AccessDenied("You do not have access to that project.")
    return project


def active_workspace_id() -> str:
    """The Fabric workspace behind the active project, or "" if unbound."""
    pid = active_project_id()
    if not pid:
        return ""
    try:
        from db.database import project_workspace_ids

        return project_workspace_ids().get(int(pid), "")
    except Exception:
        return ""


def effective_role() -> str:
    """app_users role, capped by the user's OWN access to the active workspace.

    FabricBI acts on Fabric as a shared service principal, so app_users alone would
    let a Fabric Viewer make changes the SP is allowed to make and they are not.
    Power BI's answer for their identity is the ceiling: a read-only workspace
    caps them at viewer no matter what app_users says.

    The cap only ever lowers. It cannot promote someone above their app_users
    role, so a Fabric Admin who is a FabricBI viewer stays a viewer.
    """
    role = current_role()
    wsid = active_workspace_id()
    if not wsid:
        return role
    try:
        from auth.workspace_access import for_user

        access = for_user((current_user() or {}).get("email", ""))
        if access.status == "off":
            return role
        if not access.usable or not access.can_see(wsid):
            return "viewer"
        if access.is_read_only(wsid):
            return min(role, "viewer", key=lambda r: _ROLE_RANK.get(r, 0))
    except Exception:
        return "viewer"  # fail closed: unknown Fabric access is not write access
    return role


def has_capability(minimum: str) -> bool:
    return _ROLE_RANK.get(effective_role(), 0) >= _ROLE_RANK.get(minimum, 0)


def require_capability(minimum: str = "editor", what: str = "this action") -> None:
    """Raise unless the user clears `minimum` after the Fabric cap."""
    if has_capability(minimum):
        return
    wsid = active_workspace_id()
    if wsid and effective_role() == "viewer" and current_role() != "viewer":
        raise AccessDenied(
            f"Read-only -- your Power BI access to this project's workspace is "
            f"Viewer, so {what} is not available. Ask a workspace admin for "
            f"Contributor access in Fabric."
        )
    raise AccessDenied(f"Access denied -- {what} requires the {minimum} role.")


def set_actor_for_request(email: str, ip: str) -> None:
    set_actor(user_email=email, ip_address=ip)
