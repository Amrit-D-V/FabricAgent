import base64
import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

TOKEN_CACHE_DIR = Path.home() / ".pbi_agent"
TOKEN_CACHE_FILE = TOKEN_CACHE_DIR / "token_cache.json"

PBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
SQL_SCOPE = "https://database.windows.net/.default"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"

_token_cache: dict[str, str] = {}


def _is_token_valid(token: str) -> bool:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return False
        segment = parts[1]
        segment += "=" * ((4 - len(segment) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(segment).decode("utf-8"))
        exp = payload.get("exp", 0)
        return time.time() < (exp - 300)
    except Exception:
        return False


def _get_credential():
    from azure.identity import (
        AzureCliCredential,
        ChainedTokenCredential,
        ClientSecretCredential,
    )

    chain = [AzureCliCredential()]

    from fabric.fabric_connection import get_fabric_connection

    _c = get_fabric_connection()
    client_id = _c.client_id or os.getenv("AZURE_CLIENT_ID")
    tenant_id = _c.tenant_id or os.getenv("AZURE_TENANT_ID")
    client_secret = _c.client_secret or os.getenv("AZURE_CLIENT_SECRET")
    if all([client_id, tenant_id, client_secret]):
        chain.append(
            ClientSecretCredential(
                tenant_id=tenant_id, client_id=client_id, client_secret=client_secret
            )
        )

    if len(chain) == 1 and not all([client_id, tenant_id, client_secret]):
        raise ValueError(
            "No valid credential found.\n"
            "Run  az login  in your terminal, then restart the app.\n"
            "Or set AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_CLIENT_SECRET in .env"
        )

    return ChainedTokenCredential(*chain)


def _acquire_token(scope: str) -> str:
    return _get_credential().get_token(scope).token


def _read_file_cache(scope_key: str) -> str:
    try:
        if TOKEN_CACHE_FILE.exists():
            data = json.loads(TOKEN_CACHE_FILE.read_text(encoding="utf-8"))
            return data.get(scope_key, "")
    except Exception:
        pass
    return ""


def _write_file_cache(scope_key: str, token: str):
    try:
        TOKEN_CACHE_DIR.mkdir(exist_ok=True)
        data = {}
        if TOKEN_CACHE_FILE.exists():
            try:
                data = json.loads(TOKEN_CACHE_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        data[scope_key] = token
        TOKEN_CACHE_FILE.write_text(json.dumps(data), encoding="utf-8")
    except Exception as e:
        print(f"Could not write token cache: {e}")


def _get_cached_token(scope: str, label: str) -> str:
    scope_key = scope.split("/")[2].replace(".", "_")

    cached = _token_cache.get(scope_key, "")
    if cached and _is_token_valid(cached):
        return cached

    cached = _read_file_cache(scope_key)
    if cached and _is_token_valid(cached):
        _token_cache[scope_key] = cached
        return cached

    print(f"Acquiring new {label} token...")
    token = _acquire_token(scope)
    _token_cache[scope_key] = token
    _write_file_cache(scope_key, token)
    return token


def get_token_cached() -> str:
    return _get_cached_token(PBI_SCOPE, "Power BI")


def get_sql_token() -> str:
    return _get_cached_token(SQL_SCOPE, "SQL")


def get_fabric_token() -> str:
    return _get_cached_token(FABRIC_SCOPE, "Fabric")
