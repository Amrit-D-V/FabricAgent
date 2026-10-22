from __future__ import annotations

import os
from urllib.parse import quote

import requests

from config import POWERBI_API_BASE
from util.logger_setup import get_logger

logger = get_logger(__name__)

_ADMIN_BASE = f"{POWERBI_API_BASE}/admin"
_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
_AUTHORITY = "https://login.microsoftonline.com/{tenant}"
_TIMEOUT = 30
_FABRIC_API_BASE = "https://api.fabric.microsoft.com/v1"

# The tenant throttle is 200 requests/hour and every page spends one.
_MAX_PAGES = 20

# Detail rows handed back to the model. Counts are always exact and computed
# over everything fetched; only the per-item listing is capped, because a
# broadly-shared tenant returns thousands of rows and the consumer here is an
# LLM context window, not a spreadsheet.
_MAX_ITEMS = 200

# The documented ArtifactType enum. Validated locally so a typo comes back as
# a usable message instead of an opaque 400 from the service.
_ARTIFACT_TYPES = (
    "Report",
    "PaginatedReport",
    "Dashboard",
    "Dataset",
    "Dataflow",
    "PersonalGroup",
    "Group",
    "Workspace",
    "Capacity",
    "App",
)

# Containers are split out of the item list because FabricBI's own authorization
# model keys off workspace access -- see auth/workspace_access.py and
# auth.auth.effective_role, which cap a user's role by their workspace rights.
_CONTAINERS = {"Workspace", "Group", "PersonalGroup"}

_app_cache: dict = {}


def _fail(error: str, fix: str = "", **extra) -> dict:
    out = {"ok": False, "error": error}
    if fix:
        out["fix"] = fix
    out.update(extra)
    return out


def _credentials() -> tuple[str, str, str]:
    cid = os.getenv("PBI_ADMIN_CLIENT_ID") or os.getenv("AZURE_CLIENT_ID", "")
    tid = os.getenv("PBI_ADMIN_TENANT_ID") or os.getenv("AZURE_TENANT_ID", "")
    sec = os.getenv("PBI_ADMIN_CLIENT_SECRET") or os.getenv("AZURE_CLIENT_SECRET", "")
    return cid.strip(), tid.strip(), sec.strip()


def client_credentials_token(
    client_id: str, tenant_id: str, client_secret: str
) -> tuple[str, str]:
    # (token, error) for one service principal. Public because saving an app
    # connection proves the credential before storing it, and the useful part
    # is not the token but Entra's verdict on the secret -- which is what the
    # error branches below turn into something a person can act on.
    missing = [
        name
        for name, value in (
            ("client ID", client_id),
            ("tenant ID", tenant_id),
            ("client secret", client_secret),
        )
        if not value
    ]
    if missing:
        return "", f"missing {', '.join(missing)}"

    # The secret is part of the key, never the value: an app cached under
    # tenant:client alone would keep answering with a rotated-away secret, so a
    # corrected one would appear to fail. Hashed so the cache cannot leak it.
    import hashlib

    fingerprint = hashlib.sha256(client_secret.encode()).hexdigest()[:16]
    key = f"{tenant_id}:{client_id}:{fingerprint}"
    app = _app_cache.get(key)
    if app is None:
        import msal

        app = msal.ConfidentialClientApplication(
            client_id=client_id,
            authority=_AUTHORITY.format(tenant=tenant_id),
            client_credential=client_secret,
        )
        _app_cache[key] = app

    result = app.acquire_token_for_client(scopes=[_SCOPE]) or {}
    token = result.get("access_token", "")
    if token:
        return token, ""
    return "", _token_error(result, tenant_id)


def _token() -> tuple[str, str]:
    cid, tid, sec = _credentials()
    if not (cid and tid and sec):
        _, err = client_credentials_token(cid, tid, sec)
        return "", (
            f"Admin API credentials are not configured -- {err}. "
            "Set PBI_ADMIN_CLIENT_ID / PBI_ADMIN_TENANT_ID / PBI_ADMIN_CLIENT_SECRET "
            "(or the AZURE_* equivalents) in the environment."
        )
    return client_credentials_token(cid, tid, sec)


# Never log or return the secret itself -- only Entra's verdict on it.
def _token_error(result: dict, tid: str) -> str:
    err = result.get("error", "unknown_error")
    desc = (result.get("error_description") or "").strip()[:300]
    logger.warning("client-credentials token request failed: %s", err)
    if err == "invalid_client":
        return (
            "Entra rejected the client secret (invalid_client): it is wrong, it has "
            "expired, or it belongs to a different app registration. Generate a new "
            "one under Certificates & secrets and copy the Value, not the Secret ID. "
            f"{desc}"
        )
    if err == "unauthorized_client":
        return (
            "This app registration is not authorised for the client-credentials flow "
            f"in tenant {tid}. {desc}"
        )
    if err == "invalid_request" and "tenant" in desc.lower():
        return f"Tenant {tid} was not recognised by Entra. {desc}"
    return f"Token request failed ({err}). {desc}"


def _explain(resp: requests.Response, delegated: bool = False) -> tuple[str, str]:
    body = (resp.text or "").strip()[:300]
    if delegated and resp.status_code in (401, 403):
        # The delegated token carries the caller's own tenant role, so this is
        # Entra saying "you personally are not a Fabric administrator". There
        # is nothing to fix in the app registration -- and that refusal is the
        # point of the delegated design, not a defect in it.
        return (
            (
                f"Admin API returned {resp.status_code} for your own identity -- you "
                "are not a Fabric administrator, so you cannot read another "
                "person's tenant-wide access."
            ),
            (
                "Ask someone holding the Fabric Administrator or Power BI Service "
                "Administrator role to run the review. To see your own access "
                "instead, run this without a user argument."
            ),
        )
    if resp.status_code == 401:
        # Observed against a live tenant: an unauthorised service principal gets
        # 401 here, not the 403 you might expect, and the token itself is
        # perfectly valid -- it simply carries no admin scope. So this branch,
        # not the 403 one, is where the real setup checklist belongs.
        return (
            (
                "Admin API returned 401 Unauthorized -- the token is valid but "
                "carries no tenant admin scope, so the caller is not enabled for "
                "read-only admin APIs."
            ),
            (
                "Check all three, in order: (1) Fabric admin portal -> Tenant "
                "settings -> Admin API settings -> 'Service principals can access "
                "read-only admin APIs' is Enabled and scoped to a security group; "
                "(2) this app's service principal is a member of that group; (3) the "
                "app registration has NO admin-consent Power BI *application* "
                "permissions -- for read-only admin APIs the tenant setting is the "
                "grant, and Tenant.Read.All as an application permission breaks the "
                "call rather than enabling it. To check (3), decode this app's token "
                "and look at the 'roles' claim: absent means clean, and the same app "
                "can serve both roles. If it is not clean, point the PBI_ADMIN_* "
                "triple at a dedicated registration instead."
            ),
        )
    if resp.status_code == 403:
        return (
            "Admin API returned 403 Forbidden.",
            (
                "This is almost always tenant configuration rather than code. Check "
                "both: (1) Fabric admin portal -> Admin API settings -> 'Service "
                "principals can access read-only admin APIs' is Enabled and scoped to "
                "a group holding this service principal; (2) the app registration has "
                "NO admin-consent Power BI application permissions -- for read-only "
                "admin APIs the tenant setting is the grant, and adding "
                "Tenant.Read.All as an application permission breaks it rather than "
                "enabling it. A tenant admin has to do this; FabricBI cannot grant it to "
                "itself."
            ),
        )
    if resp.status_code == 404:
        return (
            "Admin API returned 404 -- no such user in this tenant.",
            (
                "Check the UPN spelling, or pass the Entra object ID instead. "
                "External guests use the encoded form, e.g. "
                "user_other.com#EXT#@yourtenant.com."
            ),
        )
    if resp.status_code == 429:
        retry = resp.headers.get("Retry-After", "")
        return (
            (
                "Admin API returned 429 Too Many Requests -- the tenant hit the "
                "200 calls/hour limit on this endpoint."
                + (f" Retry after {retry}s." if retry else "")
            ),
            (
                "The limit is tenant-wide and shared with anything else calling admin "
                "APIs, so back off rather than retrying immediately."
            ),
        )
    if resp.status_code >= 500:
        return (
            f"Power BI returned {resp.status_code} -- a service-side error. {body}",
            "Transient. Retry in a few minutes before investigating.",
        )
    return (f"Admin API returned {resp.status_code}: {body}", "")


def _shape(
    user_id: str,
    entities: list,
    pages: int,
    hit_page_cap: bool,
    max_items: int,
    source: str = "admin-api",
    extra_notes: list | None = None,
) -> dict:
    by_type: dict[str, int] = {}
    by_right: dict[str, int] = {}
    workspaces: list[dict] = []
    items: dict[str, list] = {}

    for entry in entities:
        kind = entry.get("artifactType") or "Unknown"
        right = entry.get("accessRight") or "Unknown"
        by_type[kind] = by_type.get(kind, 0) + 1
        by_right[right] = by_right.get(right, 0) + 1
        row = {
            "id": entry.get("artifactId", ""),
            "name": entry.get("displayName", ""),
            "access": right,
        }
        if kind in _CONTAINERS:
            workspaces.append({**row, "type": kind})
        else:
            items.setdefault(kind, []).append(row)

    # Workspaces are never trimmed: there are few of them and they are the rows
    # that decide what this user can do in FabricBI.
    trimmed: dict[str, list] = {}
    shown = 0
    for kind in sorted(items):
        room = max(0, max_items - shown)
        trimmed[kind] = items[kind][:room]
        shown += len(trimmed[kind])
    total_items = sum(len(v) for v in items.values())

    notes: list[str] = list(extra_notes or [])
    if shown < total_items:
        notes.append(
            f"Item listing truncated to {shown} of {total_items} rows; the counts in "
            "`summary` are exact and cover everything fetched."
        )
    if hit_page_cap:
        notes.append(
            f"Stopped at the {pages}-page cap, so this user may hold more access than "
            "is listed. Narrow the search with artifactTypes rather than raising the "
            "cap -- the endpoint allows only 200 calls/hour tenant-wide."
        )
    if not entities and source.startswith("admin-api"):
        notes.append(
            "No items returned. This endpoint does not distinguish 'the user exists "
            "but has no access' from 'no such user', so confirm the UPN or object ID "
            "before reporting this as an access finding."
        )

    return {
        "ok": True,
        "user": user_id,
        "source": source,
        "summary": {
            "total": len(entities),
            "workspaces": len(workspaces),
            "items": total_items,
            "by_type": dict(sorted(by_type.items())),
            "by_access_right": dict(sorted(by_right.items())),
        },
        "workspaces": workspaces,
        "items": trimmed,
        "pages_fetched": pages,
        "notes": notes,
    }


def artifact_access(
    user_id: str,
    artifact_types: str = "",
    max_pages: int = _MAX_PAGES,
    max_items: int = _MAX_ITEMS,
    token: str = "",
    allow_app_identity: bool = False,
) -> dict:
    user_id = (user_id or "").strip()
    if not user_id:
        return _fail(
            "No user was given.",
            "Pass userId as a UPN (someone@company.com) or an Entra object ID.",
        )

    types_csv = ""
    if artifact_types:
        requested = [t.strip() for t in artifact_types.split(",") if t.strip()]
        valid = {t.lower(): t for t in _ARTIFACT_TYPES}
        unknown = [t for t in requested if t.lower() not in valid]
        if unknown:
            return _fail(
                f"Unknown artifact type(s): {', '.join(unknown)}.",
                f"Valid values are {', '.join(_ARTIFACT_TYPES)}.",
            )
        types_csv = ",".join(valid[t.lower()] for t in requested)

    # A caller-supplied delegated token is the normal path: the tenant role
    # travels inside it, so Entra -- not this function -- decides whether the
    # caller may read someone else. The application identity is a shared admin
    # credential that answers for everyone regardless of who is asking, so it
    # is never used unless something upstream has said so out loud.
    delegated = bool(token)
    if not delegated:
        if not allow_app_identity:
            return _fail(
                "No delegated token was supplied, so this lookup was not attempted.",
                "Tenant-wide access is read with the caller's own Entra identity. "
                "Sign in, or run 'az login', so a delegated token is available. "
                "Pass allow_app_identity=True only for an unattended operator job "
                "that is authorised to answer for the whole tenant.",
            )
        token, err = _token()
        if not token:
            return _fail(
                err,
                "This is an authentication problem, not a permissions one -- the "
                "call was never made.",
            )

    headers = {"Authorization": f"Bearer {token}"}
    # safe="" so '@' and the '#EXT#' in a guest UPN are percent-encoded. Left
    # raw, a guest UPN's '#' truncates the path at the fragment and the request
    # silently targets the wrong URL.
    url = f"{_ADMIN_BASE}/users/{quote(user_id, safe='')}/artifactAccess"
    params: dict | None = {"artifactTypes": types_csv} if types_csv else None

    entities: list = []
    pages = 0
    while url and pages < max_pages:
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=_TIMEOUT)
        except requests.RequestException as exc:
            logger.warning("admin artifactAccess transport error: %s", exc)
            return _fail(
                f"Could not reach the Power BI Admin API: {exc}",
                "Check outbound network access and any proxy configuration.",
            )

        if resp.status_code >= 400:
            error, fix = _explain(resp, delegated=delegated)
            logger.warning("admin artifactAccess %s for user", resp.status_code)
            return _fail(error, fix, status=resp.status_code)

        try:
            payload = resp.json()
        except ValueError:
            return _fail(
                "The Admin API returned a non-JSON body.",
                "Usually a sign-in page served by a proxy intercepting the call.",
            )

        # The service returns this key PascalCased ("ArtifactAccessEntities"),
        # unlike every other property in the same body. Reading only the
        # camelCase spelling turns a populated 200 into a silent empty result
        # that looks exactly like "this user has no access".
        entities.extend(
            payload.get("ArtifactAccessEntities")
            or payload.get("artifactAccessEntities")
            or []
        )
        pages += 1

        # Follow the response's own continuationUri rather than re-sending
        # continuationToken as a query param. The token comes back already
        # percent-encoded (it ends in %3D), so passing it through `params`
        # encodes the '%' again and the next page returns empty.
        url = payload.get("continuationUri") or ""
        params = None

    return _shape(
        user_id,
        entities,
        pages,
        bool(url),
        max_items,
        source="admin-api" if delegated else "admin-api-app-identity",
    )


# --- Delegated fallback -------------------------------------------------
#
# artifact_access() needs a Fabric admin or an enabled service principal, and
# in most tenants neither is available on day one. The ordinary (non-admin)
# REST endpoints answer the same question for the *caller themselves*, with no
# tenant setting and no admin consent, so this is the path that works today.
#
# It is strictly narrower: it can only ever describe the person holding the
# token. own_access() therefore refuses to stand in for anyone else -- see the
# UPN check in access_for(), which is the whole reason the token is decoded.

# (endpoint segment, ArtifactType) for the per-workspace item lists.
_DELEGATED_KINDS = (
    ("reports", "Report"),
    ("datasets", "Dataset"),
    ("dashboards", "Dashboard"),
    ("dataflows", "Dataflow"),
)


def _get_paged(
    url: str, headers: dict, max_pages: int = _MAX_PAGES
) -> tuple[list, str]:
    rows: list = []
    pages = 0
    while url and pages < max_pages:
        try:
            r = requests.get(url, headers=headers, timeout=_TIMEOUT)
        except requests.RequestException as exc:
            return rows, f"unreachable ({exc})"
        if r.status_code >= 400:
            return rows, f"returned {r.status_code}"
        try:
            payload = r.json()
        except ValueError:
            return rows, "returned a non-JSON body"
        rows.extend(payload.get("value") or [])
        pages += 1
        # As in artifact_access: follow the service's own already-encoded URI
        # rather than re-sending the token as a query param.
        url = payload.get("continuationUri") or ""
    if url:
        return rows, f"stopped at the {max_pages}-page cap (more items exist)"
    return rows, ""


# The payload segment, unverified. Reading a token Entra just issued us to see
# what it says about itself -- not to decide whether to trust it, which is the
# service's job at the other end. A malformed or opaque token yields {}, which
# every caller here treats as "nothing known", never as an assertion.
def token_claims(token: str) -> dict:
    import base64
    import json

    try:
        segment = token.split(".")[1]
        segment += "=" * ((4 - len(segment) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(segment).decode("utf-8"))
    except (AttributeError, IndexError, ValueError):
        # Deliberately narrow -- a blind except here would hide a real bug
        # behind a silent "not this user".
        return {}


def token_upn(token: str) -> str:
    claims = token_claims(token)
    for claim in ("upn", "preferred_username", "unique_name", "email"):
        value = claims.get(claim)
        if value:
            return str(value).strip().lower()
    return ""


def token_identities(token: str) -> set[str]:
    claims = token_claims(token)
    out: set[str] = set()
    for claim in ("upn", "preferred_username", "unique_name", "email", "oid", "sub"):
        value = claims.get(claim)
        if value:
            out.add(str(value).strip().lower())
    return out


def _items_via_fabric(
    fabric_token: str,
    wsid: str,
    wsname: str,
    read_only: bool,
    entities: list,
    notes: list,
) -> bool:
    rows, err = _get_paged(
        f"{_FABRIC_API_BASE}/workspaces/{wsid}/items",
        {"Authorization": f"Bearer {fabric_token}"},
    )
    if err:
        notes.append(f"{wsname}: Fabric item list {err}.")
        # Rows already collected before the failure are still real access, so
        # keep them and let the caller top up from the Power BI lists.
        if not rows:
            return False
    for row in rows:
        entities.append(
            {
                "artifactId": row.get("id", ""),
                "displayName": f"{wsname} > {row.get('displayName') or ''}",
                "artifactType": row.get("type", "Unknown"),
                "accessRight": "Read" if read_only else "ReadWrite",
            }
        )
    return True


def _items_via_powerbi(
    token: str, wsid: str, wsname: str, read_only: bool, entities: list, notes: list
) -> None:
    for segment, kind in _DELEGATED_KINDS:
        try:
            r = requests.get(
                f"{POWERBI_API_BASE}/groups/{wsid}/{segment}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=_TIMEOUT,
            )
        except requests.RequestException as exc:
            notes.append(f"{wsname}: {segment} unreachable ({exc}).")
            continue
        if r.status_code >= 400:
            # A single denied list is normal and not fatal; record it so a
            # short answer is never mistaken for a complete one.
            notes.append(f"{wsname}: {segment} returned {r.status_code}.")
            continue
        try:
            rows = r.json().get("value", []) or []
        except ValueError:
            notes.append(f"{wsname}: {segment} returned a non-JSON body.")
            continue
        for row in rows:
            entities.append(
                {
                    "artifactId": row.get("id", ""),
                    "displayName": f"{wsname} > {row.get('name') or row.get('displayName') or ''}",
                    "artifactType": kind,
                    "accessRight": "Read" if read_only else "ReadWrite",
                }
            )


def own_access(token: str, fabric_token: str = "", max_items: int = _MAX_ITEMS) -> dict:
    notes: list[str] = []
    try:
        resp = requests.get(
            f"{POWERBI_API_BASE}/groups",
            headers={"Authorization": f"Bearer {token}"},
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        return _fail(f"Could not reach the Power BI API: {exc}")
    if resp.status_code >= 400:
        error, fix = _explain(resp)
        return _fail(error, fix, status=resp.status_code)

    groups = resp.json().get("value", []) or []
    entities: list = []
    for group in groups:
        entities.append(
            {
                "artifactId": group.get("id", ""),
                "displayName": group.get("name", ""),
                "artifactType": "Workspace",
                # /groups reports capability, not a role name. Say what it is
                # rather than inventing an accessRight the API never returned.
                "accessRight": "Read" if group.get("isReadOnly") else "ReadWrite",
            }
        )

    complete = bool(fabric_token)
    for group in groups:
        wsid, wsname = group.get("id", ""), group.get("name", "")
        read_only = bool(group.get("isReadOnly"))
        if not wsid:
            continue
        if fabric_token and _items_via_fabric(
            fabric_token, wsid, wsname, read_only, entities, notes
        ):
            continue
        if fabric_token:
            complete = False  # a Fabric call failed; PBI fallback is partial
        _items_via_powerbi(token, wsid, wsname, read_only, entities, notes)

    notes.append(
        f"Enumerated as the signed-in user across {len(groups)} workspace(s) via "
        + (
            "the Fabric items API."
            if complete
            else "the Power BI per-type lists, "
            "which cover only reports, datasets, dashboards and dataflows -- "
            "lakehouses, warehouses, notebooks and data agents are NOT included."
        )
    )
    notes.append(
        "This is the caller's own access only, and rights are inherited from the "
        "workspace rather than read per item -- the Admin API is the source for true "
        "per-item rights and for anyone else's access."
    )
    return _shape(
        "", entities, 1, False, max_items, source="delegated", extra_notes=notes
    )


def access_for(
    user_id: str,
    token: str = "",
    artifact_types: str = "",
    fabric_token: str = "",
    try_admin: bool = True,
    allow_app_identity: bool = False,
) -> dict:
    # try_admin is the caller's policy decision, not a capability check. A
    # non-administrator's delegated token would be refused anyway, and every
    # attempt -- refused ones included -- spends one of the tenant's 200
    # admin calls per hour, so we do not spend one to learn what the policy
    # layer already knows.
    admin = (
        artifact_access(
            user_id,
            artifact_types=artifact_types,
            token=token,
            allow_app_identity=allow_app_identity,
        )
        if try_admin
        else _fail(
            "Tenant-wide lookup not attempted -- it is limited to administrators."
        )
    )
    if admin.get("ok"):
        return admin
    if not token:
        return admin

    holder = token_upn(token)
    identities = token_identities(token)
    if not identities or (user_id or "").strip().lower() not in identities:
        # A different person. own_access() must never stand in for them -- it
        # would report the caller's own permissions under someone else's name.
        # peer_access() is the honest substitute: it reads workspace member
        # lists the caller is already entitled to read, and is explicit that it
        # covers only shared workspaces rather than the tenant.
        peer = peer_access(token, user_id)
        if peer.get("ok"):
            peer["identity"] = f"read by {holder or 'the signed-in user'}"
            peer.setdefault("notes", []).insert(
                0,
                "Admin API unavailable ("
                + str(admin.get("error", "")).split(" -- ")[0]
                + "), so this is a workspace-membership view, not a tenant-wide one.",
            )
            return peer
        admin.setdefault("notes", []).append(
            "Neither fallback applied: the token belongs to "
            f"{holder or 'no identifiable user'}, not {user_id}, and the "
            f"workspace-membership lookup also failed ({peer.get('error', '')})."
        )
        return admin

    fallback = own_access(token, fabric_token=fabric_token)
    if fallback.get("ok"):
        fallback["user"] = user_id
        # Which credential actually produced the rows. Without this the output
        # says "delegated" but not *whose* delegation, and a reader cannot tell
        # an answer about themselves from one about the service principal.
        fallback["identity"] = f"delegated user {holder or '(unknown)'}"
        fallback.setdefault("notes", []).insert(
            0,
            "Admin API unavailable ("
            + str(admin.get("error", "")).split(" -- ")[0]
            + "), so this is the caller's own delegated view instead.",
        )
    return fallback


def cli_tokens() -> tuple[str, str]:
    try:
        from azure.identity import AzureCliCredential

        cred = AzureCliCredential()
        return (
            cred.get_token(_SCOPE).token,
            cred.get_token("https://api.fabric.microsoft.com/.default").token,
        )
    except Exception as exc:  # noqa: BLE001 - any credential failure is "no token"
        logger.debug("no Azure CLI credential available: %s", exc)
        return "", ""


def _print_report(out: dict) -> None:
    if not out.get("ok"):
        print(f"FAILED ({out.get('status', 'no HTTP call')})")
        print(f"\n  {out.get('error', '')}")
        if out.get("fix"):
            print(f"\n  FIX: {out['fix']}")
        for note in out.get("notes") or []:
            print(f"\n  NOTE: {note}")
        return

    summary = out["summary"]
    print(f"user     : {out.get('user') or '(the signed-in user)'}")
    print(f"caller   : {out.get('caller') or '(unidentified)'}")
    print(f"source   : {out.get('source')}")
    if out.get("identity"):
        print(f"read as  : {out['identity']}")
    print(
        f"total  : {summary['total']}  "
        f"({summary['workspaces']} workspace(s), {summary['items']} item(s))\n"
    )
    for kind, count in summary["by_type"].items():
        print(f"  {kind:<18} {count}")
    print(f"\nWORKSPACES ({len(out['workspaces'])})")
    for w in out["workspaces"]:
        print(f"  {w['access']:<11} {w['name']}")
    for kind, rows in out["items"].items():
        print(f"\n{kind.upper()} ({len(rows)})")
        for row in rows:
            print(f"  {row['access']:<11} {row['name']}")
    if out.get("notes"):
        print("\nNOTES")
        for note in out["notes"]:
            print(f"  - {note}")


def _main(argv: list[str]) -> int:
    import argparse
    import json as _json

    parser = argparse.ArgumentParser(
        prog="python -m fabric.admin_user_access",
        description=(
            "Report what one person can access in Fabric/Power BI, using YOUR "
            "Entra identity. FabricBI admins who also hold the Fabric Administrator "
            "role can name anyone; everyone else sees only their own access."
        ),
    )
    parser.add_argument(
        "user",
        nargs="?",
        default="",
        help="UPN or Entra object ID. Defaults to you. Naming anyone else "
        "requires the admin role.",
    )
    parser.add_argument(
        "--types",
        default="",
        help="Comma-separated artifact types, e.g. Report,Dataset (Admin API only).",
    )
    parser.add_argument("--json", action="store_true", help="Print raw JSON instead.")
    args = parser.parse_args(argv)

    # The CLI is not its own authorisation. It goes through the same policy as
    # the web and the chat tool: identity comes from the Azure CLI delegated
    # token, the role from app_users, and asking about anyone else needs both a
    # FabricBI admin role and a real Fabric Administrator role in Entra.
    from auth.access_policy import access_report

    out = access_report(args.user, artifact_types=args.types)
    if str(out.get("error") or "").startswith("REFUSED"):
        print(out["error"])
        return 1
    if args.json:
        print(_json.dumps(out, indent=2, ensure_ascii=False))
    else:
        _print_report(out)
    return 0 if out.get("ok") else 1


def peer_access(token: str, user_id: str, max_items: int = _MAX_ITEMS) -> dict:
    wanted = (user_id or "").strip().lower()
    if not wanted:
        return _fail("No user was given.")

    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.get(
            f"{POWERBI_API_BASE}/groups", headers=headers, timeout=_TIMEOUT
        )
    except requests.RequestException as exc:
        return _fail(f"Could not reach the Power BI API: {exc}")
    if resp.status_code >= 400:
        error, fix = _explain(resp)
        return _fail(error, fix, status=resp.status_code)

    groups = resp.json().get("value", []) or []
    entities: list = []
    notes: list[str] = []
    unreadable = 0

    for group in groups:
        wsid, wsname = group.get("id", ""), group.get("name", "")
        if not wsid:
            continue
        try:
            r = requests.get(
                f"{POWERBI_API_BASE}/groups/{wsid}/users",
                headers=headers,
                timeout=_TIMEOUT,
            )
        except requests.RequestException as exc:
            notes.append(f"{wsname}: member list unreachable ({exc}).")
            unreadable += 1
            continue
        if r.status_code >= 400:
            # 401/403 here means the caller is not an admin of that workspace,
            # which is normal. Counted, because it bounds how much of the
            # answer is actually known.
            notes.append(
                f"{wsname}: member list not readable ({r.status_code}) -- you are "
                "not an admin of that workspace, so this report cannot say "
                "whether they have access to it."
            )
            unreadable += 1
            continue
        try:
            members = r.json().get("value", []) or []
        except ValueError:
            notes.append(f"{wsname}: member list returned a non-JSON body.")
            unreadable += 1
            continue
        for m in members:
            ids = {
                str(m.get(k) or "").strip().lower()
                for k in ("emailAddress", "identifier", "graphId", "displayName")
            }
            if wanted not in ids:
                continue
            entities.append(
                {
                    "artifactId": wsid,
                    "displayName": wsname,
                    "artifactType": "Workspace",
                    "accessRight": m.get("groupUserAccessRight") or "Unknown",
                }
            )
            break

    notes.insert(
        0,
        f"Read from the member lists of {len(groups) - unreadable} of "
        f"{len(groups)} workspace(s) you can see. This is NOT tenant-wide: it "
        "cannot see workspaces you are not in, so an empty result means 'no "
        "access in your workspaces', not 'no access anywhere'. Only the Admin "
        "API answers the tenant-wide question.",
    )
    return _shape(
        user_id,
        entities,
        1,
        False,
        max_items,
        source="workspace-membership",
        extra_notes=notes,
    )


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
