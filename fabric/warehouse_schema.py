from __future__ import annotations

import os
import re
import struct

import pyodbc
import requests

from auth.token_manager import get_sql_token
from util.logger_setup import get_logger
from util.sql_safety import is_read_only_sql

logger = get_logger(__name__)


SCHEMA_QUERY = """
    SELECT t.TABLE_SCHEMA, t.TABLE_NAME, t.TABLE_TYPE, c.COLUMN_NAME,
           c.DATA_TYPE, c.CHARACTER_MAXIMUM_LENGTH, c.IS_NULLABLE
    FROM INFORMATION_SCHEMA.TABLES t
    JOIN INFORMATION_SCHEMA.COLUMNS c
        ON t.TABLE_NAME = c.TABLE_NAME AND t.TABLE_SCHEMA = c.TABLE_SCHEMA
    WHERE t.TABLE_TYPE IN ('BASE TABLE', 'VIEW')
      AND t.TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA', 'dbo_sys')
      AND t.TABLE_NAME NOT LIKE 'dm_db_%'
      AND t.TABLE_NAME NOT LIKE '__MSSchemaChan%'
      AND t.TABLE_NAME NOT IN ('sysdiagrams', 'database_firewall_rules')
    ORDER BY t.TABLE_TYPE DESC, t.TABLE_SCHEMA, t.TABLE_NAME, c.ORDINAL_POSITION
"""


FK_QUERY = """
    SELECT
        sch_p.name  AS parent_schema,
        tab_p.name  AS parent_table,
        col_p.name  AS parent_column,
        sch_r.name  AS referenced_schema,
        tab_r.name  AS referenced_table,
        col_r.name  AS referenced_column
    FROM sys.foreign_keys fk
    JOIN sys.foreign_key_columns fkc ON fk.object_id = fkc.constraint_object_id
    JOIN sys.tables   tab_p ON fkc.parent_object_id = tab_p.object_id
    JOIN sys.schemas  sch_p ON tab_p.schema_id = sch_p.schema_id
    JOIN sys.columns  col_p ON fkc.parent_object_id = col_p.object_id
                           AND fkc.parent_column_id = col_p.column_id
    JOIN sys.tables   tab_r ON fkc.referenced_object_id = tab_r.object_id
    JOIN sys.schemas  sch_r ON tab_r.schema_id = sch_r.schema_id
    JOIN sys.columns  col_r ON fkc.referenced_object_id = col_r.object_id
                           AND fkc.referenced_column_id = col_r.column_id
    ORDER BY sch_p.name, tab_p.name, fk.name, fkc.constraint_column_id
"""


_SQL_TYPE_TO_TMDL = {
    "int": "int64",
    "bigint": "int64",
    "smallint": "int64",
    "tinyint": "int64",
    "bit": "boolean",
    "decimal": "double",
    "numeric": "double",
    "money": "double",
    "smallmoney": "double",
    "float": "double",
    "real": "double",
    "double": "double",
    "date": "dateTime",
    "datetime": "dateTime",
    "datetime2": "dateTime",
    "smalldatetime": "dateTime",
    "datetimeoffset": "dateTime",
    "time": "dateTime",
    "varchar": "string",
    "nvarchar": "string",
    "char": "string",
    "nchar": "string",
    "text": "string",
    "ntext": "string",
    "uniqueidentifier": "string",
}


def _rows_to_ddl(rows) -> str:
    tables: dict[str, tuple[str, list[str]]] = {}
    for schema, tname, ttype, col, dtype, maxlen, nullable in rows:
        key = f"{schema}.{tname}"
        col_def = f"  {col} {dtype.upper()}"
        if maxlen and maxlen not in (None, -1):
            col_def += f"({maxlen})"
        if nullable == "NO":
            col_def += " NOT NULL"
        if key not in tables:
            tables[key] = (ttype, [])
        tables[key][1].append(col_def)
    lines = []
    for tname, (ttype, cols) in tables.items():
        keyword = "CREATE VIEW" if ttype == "VIEW" else "CREATE TABLE"
        lines.append(f"{keyword} {tname} (")
        lines.append(",\n".join(cols))
        lines.append(");\n")
    return "\n".join(lines)


def _fks_to_text(rows) -> str:
    if not rows:
        return ""
    lines = ["-- FOREIGN KEYS (use these for relationships.tmdl)"]
    for ps, pt, pc, rs, rt, rc in rows:
        lines.append(f"FK: {ps}.{pt}.{pc} -> {rs}.{rt}.{rc}")
    return "\n".join(lines) + "\n"


def _connect_service_principal(server: str, db_name: str) -> pyodbc.Connection:
    client_id = os.getenv("AZURE_CLIENT_ID", "")
    client_secret = os.getenv("AZURE_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise ValueError("AZURE_CLIENT_ID and AZURE_CLIENT_SECRET must be set in .env")
    conn_str = (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        f"SERVER={server};DATABASE={db_name};"
        "Authentication=ActiveDirectoryServicePrincipal;"
        f"UID={client_id};PWD={client_secret};"
        "Encrypt=yes;TrustServerCertificate=no;"
    )
    return pyodbc.connect(conn_str, timeout=30)


def _connect_token(server: str, db_name: str) -> pyodbc.Connection:
    token = get_sql_token()
    token_bytes = token.encode("utf-16-le")
    token_struct = struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)
    conn_str = (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        f"SERVER={server};DATABASE={db_name};"
        "Encrypt=yes;TrustServerCertificate=no;"
    )
    return pyodbc.connect(conn_str, attrs_before={1256: token_struct}, timeout=30)


def _connect_sql_auth(
    server: str, db_name: str, username: str, password: str
) -> pyodbc.Connection:
    conn_str = (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        f"SERVER={server};DATABASE={db_name};"
        f"UID={username};PWD={password};"
        "Encrypt=yes;TrustServerCertificate=no;"
    )
    return pyodbc.connect(conn_str, timeout=30)


def _connect_aad_password(
    server: str, db_name: str, username: str, password: str
) -> pyodbc.Connection:
    conn_str = (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        f"SERVER={server};DATABASE={db_name};"
        "Authentication=ActiveDirectoryPassword;"
        f"UID={username};PWD={password};"
        "Encrypt=yes;TrustServerCertificate=no;"
    )
    return pyodbc.connect(conn_str, timeout=30)


def _fetch_schema(conn: pyodbc.Connection) -> str:
    cursor = conn.cursor()
    cursor.execute(SCHEMA_QUERY)
    rows = cursor.fetchall()
    fk_rows = []
    try:
        cursor.execute(FK_QUERY)
        fk_rows = cursor.fetchall()
    except Exception as e:
        logger.warning(f"FK query failed (sys.foreign_keys may be unavailable): {e}")
    conn.close()
    ddl = _rows_to_ddl(rows)
    fk_text = _fks_to_text(fk_rows)
    return ddl + ("\n" + fk_text if fk_text else "")


def _open_conn(
    server: str, db_name: str, auth_mode: str, username: str = "", password: str = ""
) -> pyodbc.Connection:
    if auth_mode == "Service Principal (connection string)":
        return _connect_service_principal(server, db_name)
    if auth_mode == "SQL Username + Password":
        return _connect_sql_auth(server, db_name, username, password)
    if auth_mode == "AAD Username + Password (your Fabric login)":
        return _connect_aad_password(server, db_name, username, password)
    return _connect_token(server, db_name)


class SqlProbe:
    def __init__(
        self,
        server: str,
        db_name: str,
        auth_mode: str,
        username: str = "",
        password: str = "",
    ):
        self.server = server
        self.db_name = db_name
        self.auth_mode = auth_mode
        self.username = username
        self.password = password
        self.conn = None

    def __call__(self, sql: str) -> tuple[list[tuple], str | None]:
        ok, why = is_read_only_sql(sql)
        if not ok:
            return [], f"refused non-read-only probe: {why}"
        for attempt in (1, 2):
            try:
                if self.conn is None:
                    self.conn = _open_conn(
                        self.server,
                        self.db_name,
                        self.auth_mode,
                        self.username,
                        self.password,
                    )
                cur = self.conn.cursor()
                try:
                    cur.execute(sql)
                    rows = [tuple(r) for r in cur.fetchall()]
                finally:
                    cur.close()
                return rows, None
            except pyodbc.Error as e:
                sqlstate = e.args[0] if e.args else ""
                if attempt == 1 and str(sqlstate).startswith("08"):
                    self.conn = None
                    continue
                return [], str(e)
            except Exception as e:
                return [], str(e)
        return [], "probe failed"

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None


def _make_sql_probe(
    server: str, db_name: str, auth_mode: str, username: str = "", password: str = ""
) -> SqlProbe | None:
    if not (server and db_name and auth_mode):
        return None
    return SqlProbe(server, db_name, auth_mode, username, password)


def _resolve_fabric_item(
    workspace_id: str, item_id: str, token: str
) -> tuple[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    base = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}"
    for itype in ["sqldatabases", "warehouses", "mirroredWarehouses", "lakehouses"]:
        try:
            resp = requests.get(
                f"{base}/{itype}/{item_id}", headers=headers, timeout=30
            )
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            item = resp.json()
            props = item.get("properties", {})
            server = (
                props.get("serverFqdn")
                or props.get("connectionString")
                or props.get("sqlEndpointProperties", {}).get("connectionString", "")
            )
            db_name = (
                props.get("databaseName")
                or props.get("sqlEndpointProperties", {}).get("id", "")
                or item.get("displayName", "FabricDB")
            )
            if server:
                return server, db_name
        except Exception:
            pass
    raise RuntimeError(f"Could not resolve item {item_id} in workspace {workspace_id}")


def _friendly_conn_error(exc: Exception) -> str:
    low = str(exc).lower()
    if any(
        k in low for k in ("invalid_grant", "basic_action", "activedirectorypassword")
    ):
        return (
            "Sign-in blocked — 'AAD Username + Password' can't satisfy MFA or "
            "Conditional Access. Switch Authentication to 'Service Principal'."
        )
    if "login failed for user" in low:
        return (
            "Authenticated, but this identity has no access to the database. "
            "Grant the service principal access to the warehouse, then retry."
        )
    if any(
        k in low for k in ("cannot open server", "not allowed to access", "firewall")
    ):
        return (
            "Blocked by the server firewall — add this host's IP to the Fabric/SQL "
            "firewall allow-list, or double-check the server name."
        )
    return f"Connection failed: {exc}"


def fetch_schema(
    server: str,
    db_name: str,
    auth_mode: str,
    username: str = "",
    password: str = "",
) -> str:
    if auth_mode == "Service Principal (connection string)":
        conn = _connect_service_principal(server, db_name)
    elif auth_mode == "SQL Username + Password":
        conn = _connect_sql_auth(server, db_name, username, password)
    elif auth_mode == "AAD Username + Password (your Fabric login)":
        conn = _connect_aad_password(server, db_name, username, password)
    else:
        conn = _connect_token(server, db_name)
    return _fetch_schema(conn)


def _parse_schema_ddl(schema_text: str) -> dict[str, list[tuple[str, str]]]:
    result: dict[str, list[tuple[str, str]]] = {}
    table_re = re.compile(
        r"CREATE\s+TABLE\s+([^\n(]+?)\s*\((.*?)\)\s*;",
        re.DOTALL | re.IGNORECASE,
    )
    col_re = re.compile(r"^\s*\[?([A-Za-z_][\w]*)\]?\s+(\w+)", re.MULTILINE)
    for m in table_re.finditer(schema_text):
        tname = m.group(1).strip("[]").replace("[", "").replace("]", "")
        body = m.group(2)
        cols: list[tuple[str, str]] = []
        for c in col_re.finditer(body):
            col_name = c.group(1)
            sql_type = c.group(2).lower()
            cols.append((col_name, sql_type))
        if cols:
            result[tname] = cols
    return result
