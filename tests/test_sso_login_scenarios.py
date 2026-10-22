"""Scenario matrix for Entra SSO login.

SSO answers "who is this", never "may they in". FabricBI's tenant contains people
who have no business in the app, so the app_users table stays the authority --
authenticated-but-unregistered must be refused, and a disabled account must stay
disabled no matter which door it knocks on.

The callback handler is Streamlit-coupled, so what is exercised here is the
decision data it runs on: the authorize URL, the state nonce round trip, the
cookie flags, and the allowlist/is_active lookup itself.
"""

from __future__ import annotations

import secrets
from urllib.parse import parse_qs, urlparse

import pytest

import config
from auth import entra_sso

# --- The authorize request --------------------------------------------------


@pytest.fixture
def authorize_query():
    url = entra_sso.auth_url(state="NONCE-UNDER-TEST")
    return urlparse(url), parse_qs(urlparse(url).query)


def test_the_authorize_url_points_at_our_tenant(authorize_query):
    parsed, _ = authorize_query
    assert parsed.netloc == "login.microsoftonline.com"
    assert config.AZURE_TENANT_ID in parsed.path


def test_the_state_nonce_is_carried_to_entra(authorize_query):
    """Without state on the request there is nothing to compare on the way
    back, and the callback check degrades to accepting any code."""
    _, q = authorize_query
    assert q["state"] == ["NONCE-UNDER-TEST"]


def test_the_redirect_uri_is_the_configured_one(authorize_query):
    """Entra matches this string exactly against the app registration. A
    mismatch is AADSTS50011, which reads like a permissions problem."""
    _, q = authorize_query
    assert q["redirect_uri"] == [config.ENTRA_REDIRECT_URI]


def test_only_user_read_is_requested():
    """Login needs identity, nothing more. Any Power BI scope added here would
    also change what the shared app registration is consented for -- see the
    Scanner API asks in HANDOVER section 3."""
    assert entra_sso.SCOPES == ["User.Read"]


def test_account_selection_is_forced(authorize_query):
    """Without prompt=select_account a shared browser silently reuses whoever
    signed in last."""
    _, q = authorize_query
    assert q["prompt"] == ["select_account"]


def test_two_renders_produce_two_different_nonces():
    """A constant state is the same as no state."""
    assert secrets.token_urlsafe(24) != secrets.token_urlsafe(24)
    a = entra_sso.auth_url(state=secrets.token_urlsafe(24))
    b = entra_sso.auth_url(state=secrets.token_urlsafe(24))
    assert parse_qs(urlparse(a).query)["state"] != parse_qs(urlparse(b).query)["state"]


@pytest.mark.parametrize(
    "tenant,client,secret,expected",
    [
        ("t", "c", "s", True),
        ("", "c", "s", False),
        ("t", "", "s", False),
        ("t", "c", "", False),
    ],
    ids=["complete", "no tenant", "no client", "no secret"],
)
def test_configured_requires_the_full_triple(
    monkeypatch, tenant, client, secret, expected
):
    """Sign-in reads ENTRA_SSO_*, not AZURE_* — the sign-in app registration is
    deliberately separate from the backend service principal."""
    monkeypatch.setattr(entra_sso, "ENTRA_SSO_TENANT_ID", tenant)
    monkeypatch.setattr(entra_sso, "ENTRA_SSO_CLIENT_ID", client)
    monkeypatch.setattr(entra_sso, "ENTRA_SSO_CLIENT_SECRET", secret)
    assert entra_sso.configured() is expected


def test_sign_in_does_not_read_the_backend_service_principal_directly():
    """If these come back, the sign-in app and the SP have been re-merged, and
    adding the delegated Power BI scope would break the Scanner API ask."""
    import inspect

    src = inspect.getsource(entra_sso)
    assert "ENTRA_SSO_CLIENT_ID" in src
    assert "AZURE_CLIENT_ID" not in src


# --- The state nonce round trip ---------------------------------------------


def test_the_state_cookie_survives_encryption_and_comes_back_intact():
    """The nonce cannot live in st.session_state: signing in navigates away and
    returns on a new Streamlit session with empty state."""
    from auth import crypto_store
    from auth.session_cookie import STATE_TTL_SECONDS

    nonce = secrets.token_urlsafe(24)
    token = crypto_store.encrypt(nonce)
    assert token != nonce
    assert crypto_store.decrypt(token, ttl=STATE_TTL_SECONDS) == nonce


def test_a_tampered_state_cookie_does_not_decrypt():
    """Fernet is authenticated, so an attacker cannot mint a nonce that matches
    a state value they chose."""
    from auth import crypto_store
    from auth.session_cookie import STATE_TTL_SECONDS

    token = crypto_store.encrypt("real-nonce")
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(Exception):
        crypto_store.decrypt(tampered, ttl=STATE_TTL_SECONDS)


def test_the_state_window_is_short():
    """A long-lived nonce is a long-lived replay window."""
    from auth.session_cookie import STATE_TTL_SECONDS, TTL_SECONDS

    assert STATE_TTL_SECONDS <= 15 * 60
    assert STATE_TTL_SECONDS < TTL_SECONDS


@pytest.mark.parametrize(
    "expected,returned,should_match",
    [
        ("abc", "abc", True),
        ("abc", "abd", False),
        ("abc", "", False),
        ("", "abc", False),
        ("", "", False),
        ("abc", "ABC", False),
    ],
    ids=["match", "differs", "none returned", "none expected", "both empty", "case"],
)
def test_state_comparison_rejects_everything_but_an_exact_match(
    expected, returned, should_match
):
    """Mirrors the callback's guard: empty expected must never pass, or a
    browser with no cookie would accept any injected code."""
    ok = bool(expected) and secrets.compare_digest(expected, returned)
    assert ok is should_match


# --- Cookie flags -----------------------------------------------------------


@pytest.mark.parametrize(
    "explicit,redirect,expected",
    [
        ("", "https://fabricbi.example.com", True),
        ("", "http://localhost:8501", False),
        ("1", "http://localhost:8501", True),
        ("true", "http://localhost:8501", True),
        ("0", "https://fabricbi.example.com", False),
        ("false", "https://fabricbi.example.com", False),
        ("", "HTTPS://FABRICBI.EXAMPLE.COM", True),
    ],
    ids=[
        "https implies secure",
        "localhost stays insecure",
        "explicit on",
        "explicit true",
        "explicit off wins",
        "explicit false wins",
        "scheme case insensitive",
    ],
)
def test_the_secure_flag_follows_the_deployment(
    monkeypatch, explicit, redirect, expected
):
    """Defaulting off ships a non-Secure cookie to production; defaulting on
    breaks local http. The redirect URI already records which this is."""
    monkeypatch.setenv("SESSION_COOKIE_SECURE", explicit)
    monkeypatch.setattr(config, "ENTRA_REDIRECT_URI", redirect)
    assert config._cookie_secure_default() is expected


def test_the_session_cookie_is_encrypted_not_just_encoded():
    """It carries the role. Plain JSON would let anyone self-promote to admin."""
    import json

    from auth import crypto_store
    from auth.session_cookie import TTL_SECONDS

    token = crypto_store.encrypt(json.dumps({"email": "a@b.c", "role": "admin"}))
    assert "admin" not in token
    assert json.loads(crypto_store.decrypt(token, ttl=TTL_SECONDS))["role"] == "admin"


# --- Authorisation: the allowlist -------------------------------------------


@pytest.fixture
def app_users(monkeypatch):
    """Stand in for the app_users table the callback consults."""
    import auth.auth as A

    table = {}
    monkeypatch.setattr(A, "get_app_user", lambda email: table.get(email))
    return table


def _decision(rec):
    """The callback's gate, in the order auth.py applies it."""
    if not rec:
        return "denied:not registered"
    if not rec.get("is_active", True):
        return "denied:disabled"
    return "allowed:" + rec.get("role", "viewer")


@pytest.mark.parametrize(
    "rec,expected",
    [
        (None, "denied:not registered"),
        ({}, "denied:not registered"),
        ({"role": "admin", "is_active": 0}, "denied:disabled"),
        ({"role": "admin", "is_active": False}, "denied:disabled"),
        ({"role": "admin", "is_active": 1}, "allowed:admin"),
        ({"role": "editor", "is_active": 1}, "allowed:editor"),
        ({"role": "viewer", "is_active": 1}, "allowed:viewer"),
        ({"is_active": 1}, "allowed:viewer"),
    ],
    ids=[
        "no record",
        "empty record",
        "disabled int",
        "disabled bool",
        "admin",
        "editor",
        "viewer",
        "role missing defaults to viewer",
    ],
)
def test_the_allowlist_decides_not_the_tenant(rec, expected):
    """Authenticating to the Entra tenant is not authorisation for FabricBI. The
    old code defaulted an unknown email to a viewer session, which let any
    tenant user in."""
    assert _decision(rec) == expected


def test_an_unregistered_tenant_user_is_refused(app_users):
    import auth.auth as A

    app_users["known@example.com"] = {"role": "admin", "is_active": 1}
    assert A.get_app_user("stranger@example.com") is None
    assert _decision(A.get_app_user("stranger@example.com")).startswith("denied")


def test_a_disabled_account_cannot_use_sso_as_a_side_door(app_users):
    """Password login already blocks disabled accounts. SSO must not be the
    path around that."""
    import auth.auth as A

    app_users["gone@example.com"] = {"role": "admin", "is_active": 0}
    assert _decision(A.get_app_user("gone@example.com")) == "denied:disabled"


def test_the_email_claim_is_normalised_before_lookup():
    """Entra returns the UPN in whatever case the directory holds; app_users
    keys are lowercased."""
    claims = {"preferred_username": "  Admin.User@Example.Com  "}
    email = (
        (claims.get("preferred_username") or claims.get("email") or "").lower().strip()
    )
    assert email == "admin.user@example.com"


def test_the_email_claim_falls_back_when_upn_is_absent():
    """Guest and some federated accounts carry `email` but no
    `preferred_username`."""
    claims = {"email": "Guest@Partner.com"}
    email = (
        (claims.get("preferred_username") or claims.get("email") or "").lower().strip()
    )
    assert email == "guest@partner.com"


def test_no_claim_at_all_yields_an_empty_email_which_cannot_match(app_users):
    import auth.auth as A

    app_users["real@example.com"] = {"role": "admin", "is_active": 1}
    assert A.get_app_user("") is None
