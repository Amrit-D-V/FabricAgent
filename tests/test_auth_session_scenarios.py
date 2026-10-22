"""Scenario matrix for signing in and out, server-side.

The Streamlit version needed a `_LOGGED_OUT` session flag to make a logout
stick, because the cookie was written by JavaScript in a components.html iframe
that st.rerun() discarded, and st.context.cookies was parsed once at page load
so a delete that DID land stayed invisible for the rest of the session.

None of that survives the move to an HTTP API: the cookie is an ordinary
Set-Cookie header, so deleting it is just a response. What still has to hold is
the intent those tests protected -- a logout revokes the stored grant, a cookie
alone never authorises anyone, and Microsoft is the only way in -- so that is
what these pin instead.
"""

from __future__ import annotations

import pytest

import auth.auth as A
from core.context import begin, current_user, end
from core.errors import AccessDenied

ACTIVE = {"email": "a@example.com", "role": "admin", "is_active": 1}
DISABLED = {"email": "b@example.com", "role": "editor", "is_active": 0}


@pytest.fixture
def users(monkeypatch):
    table: dict[str, dict] = {}
    monkeypatch.setattr(A, "get_app_user", lambda e: table.get(e))
    return table


@pytest.fixture
def audited(monkeypatch):
    events: list[tuple] = []
    monkeypatch.setattr(A, "audit", lambda ev, **k: events.append((ev, k)))
    return events


@pytest.fixture
def ctx():
    """A request scope, so current_user() has somewhere to live."""
    token = begin()
    yield
    end(token)


# --- A cookie alone authorises nobody ---------------------------------------


def test_a_live_cookie_restores_the_session(users):
    users[ACTIVE["email"]] = dict(ACTIVE)
    user = A.user_from_session({"email": ACTIVE["email"], "name": "A"})
    assert user is not None
    assert user["email"] == ACTIVE["email"]
    assert user["role"] == "admin"


def test_the_role_comes_from_app_users_not_the_cookie(users):
    """A cookie claiming admin must not grant it -- app_users is the authority."""
    users[ACTIVE["email"]] = dict(ACTIVE, role="viewer")
    user = A.user_from_session(
        {"email": ACTIVE["email"], "name": "A", "role": "admin"}
    )
    assert user["role"] == "viewer"


def test_a_cookie_for_a_deleted_account_is_refused(users):
    assert A.user_from_session({"email": "gone@example.com"}) is None


def test_a_cookie_for_a_disabled_account_is_refused(users):
    users[DISABLED["email"]] = dict(DISABLED)
    assert A.user_from_session({"email": DISABLED["email"]}) is None


def test_no_cookie_is_no_session():
    assert A.user_from_session(None) is None
    assert A.user_from_session({}) is None


# --- Signing out revokes the grant, not just the cookie ----------------------


def test_signing_out_is_audited(audited, monkeypatch):
    monkeypatch.setattr("auth.entra_sso.sign_out", lambda e: None)
    A.logout(ACTIVE["email"])
    assert any(ev == "logout" for ev, _ in audited)


def test_signing_out_drops_the_stored_microsoft_grant(audited, monkeypatch):
    """The refresh token and cached workspace list must go too -- leaving either
    would let a later session act on access that was never re-checked."""
    revoked: list[str] = []
    monkeypatch.setattr("auth.entra_sso.sign_out", revoked.append)
    A.logout(ACTIVE["email"])
    assert revoked == [ACTIVE["email"]]


def test_signing_out_survives_a_failing_revoke(audited, monkeypatch):
    """A dead Entra call must not stop the user signing out."""
    def boom(_email):
        raise RuntimeError("entra down")

    monkeypatch.setattr("auth.entra_sso.sign_out", boom)
    A.logout(ACTIVE["email"])  # must not raise


# --- The OAuth callback is pinned to the browser that started it -------------


def test_a_mismatched_state_is_refused(audited):
    with pytest.raises(AccessDenied):
        A.complete_login("code", returned_state="attacker", expected_state="ours")


def test_a_missing_state_cookie_is_refused(audited):
    """No cookie means we cannot prove this browser started the sign-in."""
    with pytest.raises(AccessDenied):
        A.complete_login("code", returned_state="ours", expected_state="")


def test_state_mismatch_is_audited(audited):
    with pytest.raises(AccessDenied):
        A.complete_login("code", returned_state="x", expected_state="y")
    assert any(ev == "login.failure" for ev, _ in audited)


# --- Entra proves identity; app_users decides authorisation ------------------


def _entra_returns(monkeypatch, email, name="A"):
    monkeypatch.setattr(
        "auth.entra_sso.exchange_code",
        lambda code: {
            "access_token": "t",
            "id_token_claims": {"preferred_username": email, "name": name},
        },
    )


def test_an_unregistered_tenant_account_is_denied(users, audited, monkeypatch):
    """Signing in to the tenant is NOT authorisation for FabricBI."""
    _entra_returns(monkeypatch, "stranger@example.com")
    with pytest.raises(AccessDenied, match="not registered"):
        A.complete_login("code", "n", "n")
    assert any(ev == "login.denied" for ev, _ in audited)


def test_a_disabled_account_is_denied(users, audited, monkeypatch):
    users[DISABLED["email"]] = dict(DISABLED)
    _entra_returns(monkeypatch, DISABLED["email"])
    with pytest.raises(AccessDenied, match="disabled"):
        A.complete_login("code", "n", "n")
    assert any(ev == "login.blocked" for ev, _ in audited)


def test_a_registered_account_signs_in(users, audited, monkeypatch):
    users[ACTIVE["email"]] = dict(ACTIVE)
    _entra_returns(monkeypatch, ACTIVE["email"], name="Ada")
    monkeypatch.setattr("auth.workspace_access.refresh", lambda e: None)
    user = A.complete_login("code", "n", "n")
    assert user == {"email": ACTIVE["email"], "role": "admin", "name": "Ada"}
    assert any(ev == "login.success" for ev, _ in audited)


def test_a_failed_exchange_is_reported(users, audited, monkeypatch):
    monkeypatch.setattr(
        "auth.entra_sso.exchange_code",
        lambda code: {"error": "invalid_grant", "error_description": "expired"},
    )
    with pytest.raises(AccessDenied, match="expired"):
        A.complete_login("code", "n", "n")


# --- Microsoft is still the only way in --------------------------------------


def test_there_is_no_password_login_left_to_bypass_sso():
    """If any of these come back, the allowlist and the Entra checks are no
    longer the single way in."""
    for gone in ("_verify", "_process_login", "_render_login_screen"):
        assert not hasattr(A, gone), f"{gone} is back -- password login reappeared"


def test_there_is_no_mode_branch():
    """AUTH_MODE was removed on purpose: a half-set switch silently choosing how
    people get in is worse than one path that always applies."""
    import inspect

    assert "AUTH_MODE" not in inspect.getsource(A)


def test_the_auth_module_no_longer_depends_on_streamlit():
    """It has to run under the API, where there is no script run to stop."""
    import inspect

    assert "streamlit" not in inspect.getsource(A)


# --- The request scope is what carries the user now --------------------------


def test_the_signed_in_user_lives_on_the_request(ctx):
    assert current_user() is None
    token = begin(dict(ACTIVE))
    try:
        assert current_user()["email"] == ACTIVE["email"]
    finally:
        end(token)


def test_request_scopes_do_not_leak(ctx):
    """One request signing in must not sign in the next."""
    token = begin(dict(ACTIVE))
    end(token)
    assert current_user() is None
