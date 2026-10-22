from __future__ import annotations

import requests

from config import POWERBI_API_BASE

_PBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"


def sp_powerbi_token(tenant_id: str, client_id: str, client_secret: str) -> str:
    import msal

    app = msal.ConfidentialClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        client_credential=client_secret,
    )
    result = app.acquire_token_for_client(scopes=[_PBI_SCOPE]) or {}
    token = result.get("access_token")
    if not token:
        raise ValueError(
            result.get("error_description")
            or "Service principal authentication failed — check tenant / client id / secret."
        )
    return token


def _get(token: str, path: str) -> dict:
    resp = requests.get(
        f"{POWERBI_API_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def list_workspaces(token: str) -> list:
    data = _get(token, "/groups")
    return [(g.get("name", ""), g.get("id", "")) for g in data.get("value", [])]


def list_datasets(token: str, workspace_id: str) -> list:
    data = _get(token, f"/groups/{workspace_id}/datasets")
    return [(d.get("name", ""), d.get("id", "")) for d in data.get("value", [])]
