# Who may ask about whose Fabric access, and with whose credentials.
#
# One decision point for every entry path -- the web request, the MCP
# subprocess and the operator CLI -- because the previous arrangement enforced
# this only in the chat tool dispatch, and the CLI beside it answered for
# anybody who ran it.
#
# Two independent gates stand between a caller and a colleague's estate, and
# neither trusts the other:
#
#   1. the FabricBI role decides whether the tenant-wide call is attempted at all;
#   2. the delegated token decides whether it succeeds, because it carries the
#      caller's own Entra tenant role and Entra refuses the admin endpoints to
#      anyone who is not a Fabric administrator.
#
# The second gate is the one that matters. A bug in the first can no longer
# leak anything, which is the whole reason the application identity -- one
# shared credential that answers for everyone regardless of who is asking --
# is not used on this path any more.

from __future__ import annotations

import os
from dataclasses import dataclass

from fabric.admin_user_access import access_for, cli_tokens, token_identities, token_upn

TENANT_WIDE_ROLE = "admin"


@dataclass(frozen=True)
class Caller:
    email: str
    role: str
    identities: frozenset
    source: str

    def is_self(self, subject: str) -> bool:
        wanted = (subject or "").strip().lower()
        return bool(wanted) and wanted in self.identities

    @property
    def may_query_others(self) -> bool:
        return self.role == TENANT_WIDE_ROLE


def _identities(email: str, token: str) -> frozenset:
    found = set(token_identities(token)) if token else set()
    if email:
        found.add(email)
    return frozenset(found)


def _role_from_app_users(email: str) -> str:
    if not email:
        return "viewer"
    try:
        from db.database import get_app_user

        rec = get_app_user(email) or {}
    except Exception:
        return "viewer"
    if not rec or not rec.get("is_active", True):
        return "viewer"
    return str(rec.get("role") or "viewer").strip().lower()


# (email, role, source) for whoever is driving this call, before any token is
# involved. Empty email means an operator CLI, whose identity can only come
# from the token itself.
def _principal() -> tuple[str, str, str]:
    try:
        from core.context import current_user

        user = current_user() or {}
    except Exception:
        user = {}
    if user:
        # effective_role only ever lowers the app_users role, so reading it
        # here can fail closed but can never promote anyone.
        try:
            from auth.auth import effective_role

            role = effective_role()
        except Exception:
            role = "viewer"
        return (user.get("email") or "").strip().lower(), role, "session"

    # MCP subprocess: the request ContextVar cannot cross the process
    # boundary, so api.chat passes the caller in the child's environment --
    # never as a tool argument the model could write.
    env_email = (os.getenv("FABRICBI_CALLER_EMAIL") or "").strip().lower()
    env_role = (os.getenv("FABRICBI_CALLER_ROLE") or "").strip().lower()
    if env_email or env_role:
        return env_email, env_role or "viewer", "mcp-env"
    return "", "", "cli-token"


# The caller's OWN delegated tokens. Never the application identity: these are
# what make Entra the enforcement point rather than this process.
def caller_tokens(email: str = "") -> tuple[str, str]:
    if not email:
        return cli_tokens()

    from auth import entra_sso

    # Admin scope first so an administrator gets a token that can answer the
    # tenant-wide question; the workspace scope is what everyone else needs,
    # and asking for both at once would fail for anyone missing either.
    token = ""
    for acquire in (entra_sso.acquire_admin_token, entra_sso.acquire_powerbi_token):
        token, _ = acquire(email)
        if token:
            break
    fabric_token, _ = entra_sso.acquire_fabric_token(email)
    return token, fabric_token


def resolve_caller(token: str = "") -> Caller:
    email, role, source = _principal()
    if source == "cli-token":
        # Running the script is no longer its own authorisation. Identity comes
        # from the delegated token, and the role is read from app_users exactly
        # as it is on the web rather than assumed from having a shell.
        email = token_upn(token) if token else ""
        role = _role_from_app_users(email)
    return Caller(email, role, _identities(email, token), source)


# (subject to read, may the tenant-wide API be tried, refusal or "")
def authorize(subject: str, caller: Caller) -> tuple[str, bool, str]:
    wanted = (subject or "").strip().lower()
    if not wanted:
        # Naming no subject is a request about oneself, which needs no role.
        return caller.email, caller.may_query_others, ""
    if caller.is_self(wanted):
        return wanted, caller.may_query_others, ""
    if caller.may_query_others:
        return wanted, True, ""
    if not caller.email:
        return (
            wanted,
            False,
            "REFUSED — no signed-in identity, so this lookup cannot be attributed "
            "to anyone. Sign in, or run 'az login', and try again.",
        )
    return (
        wanted,
        False,
        f"REFUSED — you can only look up your own access ({caller.email}). "
        "Reporting another person's Fabric permissions is restricted to FabricBI "
        "admins holding the Fabric Administrator role. Ask an admin to run the "
        "access review.",
    )


def access_report(
    subject: str = "",
    artifact_types: str = "",
    pbi_token: str = "",
    fabric_token: str = "",
) -> dict:
    if not pbi_token:
        pbi_token, acquired_fabric = caller_tokens(_principal()[0])
        fabric_token = fabric_token or acquired_fabric

    caller = resolve_caller(pbi_token)
    target, try_admin, refusal = authorize(subject, caller)
    if refusal:
        _audit(caller, subject, "denied")
        return {
            "ok": False,
            "error": refusal,
            "subject": subject,
            "caller": caller.email,
        }
    if not target:
        return {
            "ok": False,
            "error": (
                "REFUSED — no user was named and no signed-in identity was found "
                "to default to."
            ),
            "fix": "Sign in, or run 'az login', so the caller can be identified.",
        }

    out = access_for(
        target,
        token=pbi_token,
        fabric_token=fabric_token,
        artifact_types=artifact_types,
        try_admin=try_admin,
    )
    # Reading someone else's estate is an administrative act and is recorded as
    # one, whichever entry path was used.
    if not caller.is_self(target):
        _audit(caller, target, "success" if out.get("ok") else "failure")
    out["caller"] = caller.email
    return out


def _audit(caller: Caller, subject: str, outcome: str) -> None:
    try:
        from db.database import audit

        audit(
            "access.review",
            user_email=caller.email,
            target=subject,
            outcome=outcome,
            severity="warning" if outcome == "denied" else "info",
            details=f"via {caller.source} as {caller.role or 'unknown'}",
        )
    except Exception:
        pass
