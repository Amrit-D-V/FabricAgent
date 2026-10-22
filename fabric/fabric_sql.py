from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class FetchedSchema:

    server: str
    database: str
    tables: list[tuple[str, str, int]] = field(default_factory=list)
    columns: list[tuple[str, str, str, str]] = field(default_factory=list)
    fetch_seconds: float = 0.0
    error: str = ""

    @property
    def table_count(self) -> int:
        return len(self.tables)

    @property
    def column_count(self) -> int:
        return len(self.columns)

    def summary(self) -> str:
        return (
            f"{self.database} @ {self.server.split('.')[0]}…: "
            f"{self.table_count} tables, {self.column_count} columns "
            f"(fetched in {self.fetch_seconds:.1f}s)"
        )

    def to_prompt_block(self, max_chars: int = 4000) -> str:
        lines = [f"## Underlying database schema — {self.database}"]
        lines.append(f"_Server:_ `{self.server}`")
        lines.append(
            f"_Tables:_ **{self.table_count}** · _Columns:_ **{self.column_count}**"
        )
        lines.append("")
        cur_table = None
        for schema, table, col, col_type in self.columns:
            full = f"{schema}.{table}"
            if full != cur_table:
                lines.append(f"\n**{full}**")
                cur_table = full
            lines.append(f"  - `{col}`  *{col_type}*")
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n\n_… schema truncated for prompt budget._"
        return text


_SCHEMA_QUERY = """
    SELECT t.TABLE_SCHEMA, t.TABLE_NAME, c.COLUMN_NAME, c.DATA_TYPE
    FROM INFORMATION_SCHEMA.TABLES t
    JOIN INFORMATION_SCHEMA.COLUMNS c
        ON t.TABLE_NAME = c.TABLE_NAME
       AND t.TABLE_SCHEMA = c.TABLE_SCHEMA
    WHERE t.TABLE_TYPE IN ('BASE TABLE', 'VIEW')
      AND t.TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA', 'dbo_sys')
      AND t.TABLE_NAME NOT LIKE 'dm_db_%'
      AND t.TABLE_NAME NOT LIKE '__MSSchemaChan%'
    ORDER BY t.TABLE_SCHEMA, t.TABLE_NAME, c.ORDINAL_POSITION
"""


_SQL_COPT_SS_ACCESS_TOKEN = 1256


_PREFERRED_DRIVERS = (
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
)


def installed_drivers() -> list[str]:
    try:
        import pyodbc

        return list(pyodbc.drivers())
    except Exception:
        return []


def pick_driver() -> str:
    available = installed_drivers()
    for name in _PREFERRED_DRIVERS:
        if name in available:
            return name
    return ""


def driver_error() -> str:
    available = installed_drivers()
    found = ", ".join(available) if available else "none"
    return (
        "No Microsoft ODBC Driver for SQL Server (17 or 18) is installed — "
        "Fabric and Azure SQL need one, and Azure AD auth needs 17+. "
        f"Drivers found on this machine: {found}. Install Driver 18 from "
        "https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server"
    )


def _common_conn_str(server: str, database: str) -> str:
    driver = pick_driver()
    if not driver:
        raise RuntimeError(driver_error())
    return (
        f"DRIVER={{{driver}}};"
        f"SERVER={server};DATABASE={database};"
        "Encrypt=yes;TrustServerCertificate=no;"
    )


def _connect_aad_password(server: str, database: str, email: str, password: str):
    import pyodbc

    conn_str = (
        _common_conn_str(server, database)
        + "Authentication=ActiveDirectoryPassword;"
        + f"UID={email};PWD={password};"
    )
    return pyodbc.connect(conn_str, timeout=30)


def _connect_service_principal(server: str, database: str):
    import os

    import pyodbc

    client_id = os.getenv("AZURE_CLIENT_ID", "")
    tenant_id = os.getenv("AZURE_TENANT_ID", "")
    client_secret = os.getenv("AZURE_CLIENT_SECRET", "")
    if not all([client_id, tenant_id, client_secret]):
        raise RuntimeError(
            "Service Principal auth requires AZURE_CLIENT_ID, AZURE_TENANT_ID, "
            "and AZURE_CLIENT_SECRET in .env."
        )
    conn_str = (
        _common_conn_str(server, database)
        + "Authentication=ActiveDirectoryServicePrincipal;"
        + f"UID={client_id}@{tenant_id};PWD={client_secret};"
    )
    return pyodbc.connect(conn_str, timeout=30)


def _connect_sql_auth(server: str, database: str, username: str, password: str):
    import pyodbc

    conn_str = _common_conn_str(server, database) + f"UID={username};PWD={password};"
    return pyodbc.connect(conn_str, timeout=30)


def _decode_token_identity(token: str) -> dict:
    import base64
    import json

    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        seg = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(seg).decode("utf-8"))
        return {
            "upn": payload.get("upn") or payload.get("unique_name") or "",
            "appid": payload.get("appid", ""),
            "oid": payload.get("oid", ""),
            "idtyp": payload.get("idtyp", ""),
            "aud": payload.get("aud", ""),
        }
    except Exception:
        return {}


def _clear_sql_token_cache():
    try:
        import json

        from auth.token_manager import SQL_SCOPE, TOKEN_CACHE_FILE, _token_cache

        scope_key = SQL_SCOPE.split("/")[2].replace(".", "_")
        _token_cache.pop(scope_key, None)
        if TOKEN_CACHE_FILE.exists():
            try:
                data = json.loads(TOKEN_CACHE_FILE.read_text(encoding="utf-8"))
                data.pop(scope_key, None)
                TOKEN_CACHE_FILE.write_text(json.dumps(data), encoding="utf-8")
            except Exception:
                pass
    except Exception:
        pass


def _make_token_struct(token: str):
    token_bytes = token.encode("utf-16-le")
    return struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)


def _connect_aad_token(server: str, database: str):
    import time

    import pyodbc

    from auth.token_manager import get_sql_token

    token = get_sql_token()
    token_struct = _make_token_struct(token)

    last_err = None
    for attempt in range(2):
        try:
            return pyodbc.connect(
                _common_conn_str(server, database),
                attrs_before={_SQL_COPT_SS_ACCESS_TOKEN: token_struct},
                timeout=30,
            )
        except Exception as e:
            last_err = e
            err_str = str(e)
            if attempt == 0 and ("40532" in err_str or "paused" in err_str.lower()):
                _clear_sql_token_cache()
                token = get_sql_token()
                token_struct = _make_token_struct(token)
                time.sleep(5)
                continue
            break

    if last_err is None:
        raise RuntimeError("SQL connection failed without exception")

    e = last_err
    ident = _decode_token_identity(token)
    err_str = str(e)
    if ident:
        tag = (
            f"User: {ident.get('upn')}"
            if ident.get("idtyp") == "user"
            else f"Service Principal (appid={ident.get('appid')})"
        )
        principal_name = (
            ident.get("upn") if ident.get("idtyp") == "user" else ident.get("appid")
        )

        if "40532" in err_str:
            fix = (
                f"FIX (40532 = identity authenticated but lacks DB-level access):\n"
                f"  1. Open Fabric portal → workspace containing '{database}'\n"
                f"  2. Click the '{database}' SQL Database item\n"
                f"  3. Toolbar: Security → Manage SQL security (or open the\n"
                f"     query editor and run T-SQL below as a SQL admin)\n"
                f"  4. Add '{principal_name}' with at least db_datareader role.\n"
                f"\n"
                f"  T-SQL to grant (run in the '{database}' DB as a SQL admin):\n"
                f"    CREATE USER [{principal_name}] FROM EXTERNAL PROVIDER;\n"
                f"    ALTER ROLE db_datareader ADD MEMBER [{principal_name}];\n"
                f"\n"
                f"  If you ARE the SQL admin and still see 40532, the DB may\n"
                f"  have been paused — wait 30s and retry once."
            )
            raise RuntimeError(
                f"[{tag}] 40532 Login failed for DB '{database}'\n\n{fix}\n\n--- Original error ---\n{err_str}"
            ) from e

        cause = ""
        if ident.get("idtyp") == "app":
            cause = (
                " | Cause: SP not granted access to this DB. "
                "Run `az login` then restart, OR grant the SP DB access in Fabric."
            )
        prefix = f"[Identity: {tag} | aud={ident.get('aud', '')}{cause}] "
        raise RuntimeError(prefix + err_str) from e
    raise e


AUTH_AAD_PASSWORD = "AAD Username + Password (your Fabric login)"
AUTH_SERVICE_PRINCIPAL = "Service Principal (connection string)"
AUTH_SQL = "SQL Username + Password"
AUTH_AAD_TOKEN = "Azure AD Token (database.windows.net)"


AUTH_MODES_BY_ID = {
    "aad_token": AUTH_AAD_TOKEN,
    "aad_password": AUTH_AAD_PASSWORD,
    "sql": AUTH_SQL,
    "service_principal": AUTH_SERVICE_PRINCIPAL,
}


def normalize_auth_mode(value: str) -> str:
    """Accept API identifiers and the display names used by saved connections."""
    mode = AUTH_MODES_BY_ID.get(value, value)
    if mode not in AUTH_MODES_BY_ID.values():
        raise ValueError(f"Unknown auth mode: {value!r}")
    return mode


def _open_connection(
    server: str,
    database: str,
    auth_mode: str,
    username: str = "",
    password: str = "",
):
    auth_mode = normalize_auth_mode(auth_mode)
    if auth_mode == AUTH_SERVICE_PRINCIPAL:
        return _connect_service_principal(server, database)
    if auth_mode == AUTH_SQL:
        return _connect_sql_auth(server, database, username, password)
    if auth_mode == AUTH_AAD_PASSWORD:
        return _connect_aad_password(server, database, username, password)
    if auth_mode == AUTH_AAD_TOKEN:
        return _connect_aad_token(server, database)
    raise ValueError(f"Unknown auth mode: {auth_mode!r}")


def fetch_schema(
    server: str,
    database: str,
    auth_mode: str = AUTH_AAD_TOKEN,
    username: str = "",
    password: str = "",
) -> FetchedSchema:
    import time

    try:
        import pyodbc  # noqa: F401 — availability probe only
    except ImportError:
        return FetchedSchema(
            server=server,
            database=database,
            error="pyodbc is not installed. Run: pip install pyodbc",
        )

    out = FetchedSchema(server=server, database=database)
    t0 = time.time()
    try:
        conn = _open_connection(server, database, auth_mode, username, password)
    except Exception as e:
        msg = str(e)
        if "data source name not found" in msg.lower() or "im002" in msg.lower():
            msg = driver_error()
        out.error = f"Connect failed: {msg[:1200]}"
        return out

    try:
        cursor = conn.cursor()
        cursor.execute(_SCHEMA_QUERY)
        for row in cursor.fetchall():
            out.columns.append((row[0], row[1], row[2], row[3]))
    except Exception as e:
        out.error = f"INFORMATION_SCHEMA query failed: {str(e)[:300]}"
        return out
    finally:
        try:
            conn.close()
        except Exception:
            pass

    from collections import Counter

    table_cols = Counter((s, t) for s, t, _, _ in out.columns)
    out.tables = sorted([(s, t, n) for (s, t), n in table_cols.items()])
    out.fetch_seconds = time.time() - t0
    logger.info(out.summary())
    return out
