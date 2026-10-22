import hashlib
import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Read .env HERE rather than trusting the entry point to have done it. The
# constants below are resolved at import time, and this module is reached
# through chains that have not loaded .env yet: auth/auth.py imports
# db.database directly, and app.py imports auth.auth at line 48 -- 34 lines
# before its own load_dotenv().
#
# The failure was silent and expensive: DB_BACKEND fell back to "sqlite" and
# the app kept reading and writing project_data.db while .env said fabric.
# Nothing errored, so it looked like the cutover had worked.
#
# load_dotenv does not overwrite variables already in the environment, so a
# real export still wins.
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

DB_PATH = os.getenv("DB_PATH", os.path.join(_PROJECT_ROOT, "project_data.db"))

# Which store backs the app's own tables. "sqlite" keeps the local file;
# "fabric" routes every _get_connection() to the Fabric SQL Database in
# FABRIC_APP_DB_SERVER / FABRIC_APP_DB_NAME.
#
# db/fabric_backend.py deliberately mirrors _SqliteConn / _SqliteCursor, so the
# ~47 modules importing db.database need no change -- the swap happens here and
# nowhere else. Default stays sqlite so this is opt-in.
#
# An unrecognised value raises instead of falling back. Falling back is how the
# bug above stayed invisible: "farbic" in .env must not quietly mean local.
_BACKENDS = {"sqlite", "fabric"}
DB_BACKEND = os.getenv("DB_BACKEND", "sqlite").strip().lower()
if DB_BACKEND not in _BACKENDS:
    raise ValueError(
        "DB_BACKEND={!r} is not one of {}".format(
            DB_BACKEND, ", ".join(sorted(_BACKENDS))
        )
    )
UPLOADS_DIR = os.getenv("UPLOADS_DIR", os.path.join(_PROJECT_ROOT, "uploads"))

# WAL is the right default on a local disk, but it CANNOT be used on a network
# filesystem: it coordinates readers through a shared-memory -shm file that SMB
# and NFS do not support, so an Azure Files mount fails or silently degrades.
# Deployments that mount the database on a share set SQLITE_JOURNAL_MODE=DELETE.
# Only one writer exists either way, so WAL's concurrency edge is not lost.
_JOURNAL_MODES = {"WAL", "DELETE", "TRUNCATE", "PERSIST", "MEMORY", "OFF"}
SQLITE_JOURNAL_MODE = os.getenv("SQLITE_JOURNAL_MODE", "WAL").strip().upper()
if SQLITE_JOURNAL_MODE not in _JOURNAL_MODES:
    raise ValueError(
        "SQLITE_JOURNAL_MODE={!r} is not one of {}".format(SQLITE_JOURNAL_MODE, ", ".join(sorted(_JOURNAL_MODES)))
    )


@dataclass
class Client:
    id: int | None = None
    name: str = ""
    description: str = ""
    metadata: dict[str, Any] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


@dataclass
class Project:
    id: int | None = None
    client_id: int = 0
    name: str = ""
    description: str = ""
    metadata: dict[str, Any] = None
    created_at: str = ""

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}
        if not self.created_at:
            self.created_at = datetime.now().isoformat()


@dataclass
class Document:
    id: int | None = None
    project_id: int = 0
    filename: str = ""
    original_name: str = ""
    file_type: str = ""
    file_size: int = 0
    file_path: str = ""
    description: str = ""
    uploaded_at: str = ""

    def __post_init__(self):
        if not self.uploaded_at:
            self.uploaded_at = datetime.now().isoformat()


@dataclass
class Wireframe:
    id: int | None = None
    project_id: int = 0
    title: str = ""
    description: str = ""
    spec: dict[str, Any] = None
    image: str = ""
    status: str = "draft"
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self):
        if self.spec is None:
            self.spec = {}
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        if not self.updated_at:
            self.updated_at = datetime.now().isoformat()


class _SqliteCursor:
    def __init__(self, cur):
        self._c = cur

    def execute(self, sql, parameters=()):
        self._c.execute(sql, parameters)
        return self

    def fetchone(self):
        row = self._c.fetchone()
        return dict(row) if row else None

    def fetchall(self):
        return [dict(r) for r in self._c.fetchall()]

    @property
    def rowcount(self):
        return self._c.rowcount

    @property
    def lastrowid(self):
        return self._c.lastrowid


class _SqliteConn:
    def __init__(self, conn):
        self._c = conn

    def cursor(self):
        return _SqliteCursor(self._c.cursor())

    def execute(self, sql, parameters=()):
        cur = self.cursor()
        cur.execute(sql, parameters)
        return cur

    def commit(self):
        self._c.commit()

    def close(self):
        try:
            self._c.rollback()
        except sqlite3.Error:
            pass


_thread_local = threading.local()


def _get_connection():
    if DB_BACKEND == "fabric":
        from db import fabric_backend

        return fabric_backend.get_connection()

    conn = getattr(_thread_local, "conn", None)
    if conn is None or getattr(_thread_local, "path", None) != DB_PATH:
        from config import SQLITE_BUSY_TIMEOUT_S

        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

        parent = os.path.dirname(os.path.abspath(DB_PATH))
        if parent:
            os.makedirs(parent, exist_ok=True)

        conn = sqlite3.connect(DB_PATH, timeout=SQLITE_BUSY_TIMEOUT_S)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA journal_mode={SQLITE_JOURNAL_MODE}")
        conn.execute(f"PRAGMA busy_timeout = {int(SQLITE_BUSY_TIMEOUT_S * 1000)}")
        _thread_local.conn = conn
        _thread_local.path = DB_PATH
    return _SqliteConn(conn)


@contextmanager
def _db_cursor():
    conn = _get_connection()
    try:
        yield conn.cursor()
        conn.commit()
    finally:
        conn.close()


def _ddl(cursor, sql: str) -> None:
    try:
        cursor.execute(sql)
    except sqlite3.OperationalError as e:
        msg = str(e).lower()
        if "already exists" not in msg and "duplicate column" not in msg:
            raise


_db_initialized_path = None


def init_db():
    global _db_initialized_path
    if DB_BACKEND == "fabric":
        # The DDL below is SQLite dialect (CREATE TABLE IF NOT EXISTS,
        # AUTOINCREMENT) which T-SQL rejects. The Fabric schema is created
        # and migrated out of band -- see scripts/migrate_to_fabric.py.
        return
    if _db_initialized_path == DB_PATH:
        return

    conn = _get_connection()
    try:
        cursor = conn.cursor()

        _ddl(
            cursor,
            """
            CREATE TABLE IF NOT EXISTS clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT,
                metadata TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """,
        )

        _ddl(
            cursor,
            """
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                metadata TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (client_id) REFERENCES clients (id),
                UNIQUE(client_id, name)
            )
        """,
        )

        _ddl(
            cursor,
            """
            CREATE TABLE IF NOT EXISTS wireframes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                spec TEXT,
                image TEXT,
                status TEXT DEFAULT 'draft',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects (id)
            )
        """,
        )

        _ddl(
            cursor,
            """
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                original_name TEXT NOT NULL,
                file_type TEXT,
                file_size INTEGER,
                file_path TEXT NOT NULL,
                description TEXT,
                uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects (id)
            )
        """,
        )

        _ddl(
            cursor,
            """
            CREATE TABLE IF NOT EXISTS semantic_models (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                wireframe_id INTEGER,
                model_name TEXT NOT NULL,
                server_fqdn TEXT,
                db_name TEXT,
                tmdl_files TEXT,
                pbip_path TEXT,
                status TEXT DEFAULT 'generated',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects (id),
                FOREIGN KEY (wireframe_id) REFERENCES wireframes (id)
            )
        """,
        )

        _ddl(
            cursor,
            """
            CREATE TABLE IF NOT EXISTS api_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                agent TEXT NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0.0,
                latency_ms INTEGER DEFAULT 0,
                status TEXT DEFAULT 'success',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects (id)
            )
        """,
        )

        _ddl(
            cursor,
            """
            CREATE TABLE IF NOT EXISTS project_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                rule_text TEXT NOT NULL,
                is_active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects (id)
            )
        """,
        )

        _ddl(
            cursor,
            """
            CREATE TABLE IF NOT EXISTS app_users (
                email TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'editor',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """,
        )
        _ddl(
            cursor,
            "ALTER TABLE app_users ADD COLUMN role TEXT NOT NULL DEFAULT 'editor'",
        )
        _ddl(
            cursor,
            "ALTER TABLE app_users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1",
        )
        _ddl(cursor, "ALTER TABLE projects ADD COLUMN git_remote TEXT")

        _ddl(
            cursor,
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                user_email TEXT,
                event_type TEXT NOT NULL,
                target TEXT,
                details TEXT
            )
        """,
        )
        _ddl(
            cursor,
            "CREATE INDEX IF NOT EXISTS idx_audit_created_at ON audit_log(created_at DESC)",
        )
        for _col, _decl in (
            ("severity", "TEXT DEFAULT 'info'"),
            ("outcome", "TEXT DEFAULT 'success'"),
            ("ip_address", "TEXT DEFAULT ''"),
            ("project_id", "INTEGER"),
            ("project_name", "TEXT DEFAULT ''"),
        ):
            _ddl(cursor, f"ALTER TABLE audit_log ADD COLUMN {_col} {_decl}")

        _ddl(
            cursor,
            """
            CREATE TABLE IF NOT EXISTS chat_history (
                project_id INTEGER PRIMARY KEY,
                messages_json TEXT,
                claude_history_json TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """,
        )

        _ddl(
            cursor,
            """
            CREATE TABLE IF NOT EXISTS chat_history_v2 (
                project_id INTEGER NOT NULL,
                user_email TEXT NOT NULL DEFAULT '',
                messages_json TEXT,
                claude_history_json TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (project_id, user_email)
            )
        """,
        )

        _ddl(
            cursor,
            """
            CREATE TABLE IF NOT EXISTS org_llm_keys (
                provider TEXT PRIMARY KEY,
                key_encrypted TEXT NOT NULL,
                updated_at TEXT,
                updated_by TEXT DEFAULT ''
            )
        """,
        )

        _ddl(
            cursor,
            """
            CREATE TABLE IF NOT EXISTS fabric_connection (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                tenant_id TEXT NOT NULL,
                client_id TEXT NOT NULL,
                secret_encrypted TEXT NOT NULL,
                workspace_name TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                models_json TEXT NOT NULL,
                catalogs_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT,
                updated_by TEXT DEFAULT ''
            )
        """,
        )

        # Connection LIBRARY: many saved Fabric accounts, each project binds to
        # one (projects.fabric_connection_id). The legacy single-row
        # `fabric_connection` table above is migrated into this one below and
        # then left untouched for rollback.
        _ddl(
            cursor,
            """
            CREATE TABLE IF NOT EXISTS fabric_connections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                tenant_id TEXT NOT NULL,
                client_id TEXT NOT NULL,
                secret_encrypted TEXT NOT NULL,
                workspace_name TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                models_json TEXT NOT NULL DEFAULT '[]',
                catalogs_json TEXT NOT NULL DEFAULT '{}',
                is_default INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT,
                updated_by TEXT DEFAULT ''
            )
        """,
        )
        _ddl(cursor, "ALTER TABLE projects ADD COLUMN fabric_connection_id INTEGER")

        # One-time migration of the legacy single row into the library.
        try:
            _legacy = cursor.execute(
                "SELECT tenant_id, client_id, secret_encrypted, workspace_name, "
                "workspace_id, models_json, catalogs_json, updated_at, updated_by "
                "FROM fabric_connection WHERE id = 1"
            ).fetchone()
            _have = cursor.execute(
                "SELECT COUNT(*) AS n FROM fabric_connections"
            ).fetchone()
            if _legacy and not (_have["n"] if _have else 0):
                cursor.execute(
                    "INSERT INTO fabric_connections "
                    "(name, tenant_id, client_id, secret_encrypted, workspace_name, "
                    " workspace_id, models_json, catalogs_json, is_default, "
                    " updated_at, updated_by) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                    (
                        _legacy["workspace_name"] or "Default",
                        _legacy["tenant_id"],
                        _legacy["client_id"],
                        _legacy["secret_encrypted"],
                        _legacy["workspace_name"],
                        _legacy["workspace_id"],
                        _legacy["models_json"],
                        _legacy["catalogs_json"],
                        _legacy["updated_at"],
                        _legacy["updated_by"],
                    ),
                )
        except sqlite3.OperationalError:
            pass  # legacy table absent on a fresh install

        # Which Fabric workspaces each user can actually see, as Power BI
        # reported it for THEIR identity. This is the gate's source of truth;
        # it is a cache of a remote fact, never an authority in its own right,
        # so every read is paired with checked_at.
        _ddl(
            cursor,
            """
            CREATE TABLE IF NOT EXISTS user_workspace_access (
                user_email TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                workspace_name TEXT DEFAULT '',
                is_read_only INTEGER NOT NULL DEFAULT 0,
                checked_at TEXT NOT NULL,
                PRIMARY KEY (user_email, workspace_id)
            )
        """,
        )
        _ddl(
            cursor,
            "CREATE INDEX IF NOT EXISTS idx_uwa_email ON user_workspace_access(user_email)",
        )

        # The user's MSAL token cache, encrypted at rest
        _ddl(
            cursor,
            """
            CREATE TABLE IF NOT EXISTS user_token_cache (
                user_email TEXT PRIMARY KEY,
                cache_encrypted TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """,
        )

        _ddl(
            cursor,
            """
            CREATE TABLE IF NOT EXISTS project_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                user_email TEXT NOT NULL,
                role TEXT DEFAULT 'viewer',
                added_by TEXT DEFAULT '',
                added_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(project_id, user_email),
                FOREIGN KEY (project_id) REFERENCES projects (id)
            )
        """,
        )

        _ddl(
            cursor,
            """
            CREATE TABLE IF NOT EXISTS custom_skills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                body TEXT NOT NULL,
                created_by TEXT DEFAULT '',
                is_active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """,
        )

        _ddl(
            cursor,
            """
            CREATE TABLE IF NOT EXISTS project_skills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                skill_slug TEXT NOT NULL,
                is_enabled INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects (id),
                UNIQUE (project_id, skill_slug)
            )
        """,
        )

        _ddl(
            cursor,
            "CREATE INDEX IF NOT EXISTS idx_projects_client_id ON projects(client_id)",
        )
        _ddl(
            cursor,
            "CREATE INDEX IF NOT EXISTS idx_wireframes_project_id ON wireframes(project_id)",
        )
        _ddl(
            cursor,
            "CREATE INDEX IF NOT EXISTS idx_documents_project_id ON documents(project_id)",
        )
        _ddl(
            cursor,
            "CREATE INDEX IF NOT EXISTS idx_semantic_models_project_id ON semantic_models(project_id)",
        )
        _ddl(
            cursor,
            "CREATE INDEX IF NOT EXISTS idx_project_rules_project_id ON project_rules(project_id)",
        )
        _ddl(
            cursor,
            "CREATE INDEX IF NOT EXISTS idx_audit_log_project_id ON audit_log(project_id)",
        )

        os.makedirs(UPLOADS_DIR, exist_ok=True)

        conn.commit()
    finally:
        conn.close()
    _db_initialized_path = DB_PATH


def save_chat_state(
    project_id: int, messages: list, claude_history: list, user_email: str = ""
) -> None:
    slim = []
    for m in messages:
        m2 = dict(m)
        if m2.get("image_data"):
            m2["image_data"] = None
            m2["image_type"] = None
        slim.append(m2)
    with _db_cursor() as cursor:
        cursor.execute(
            "INSERT OR REPLACE INTO chat_history_v2 "
            "(project_id, user_email, messages_json, claude_history_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                project_id,
                (user_email or "").lower().strip(),
                json.dumps(slim),
                json.dumps(claude_history),
                datetime.now().isoformat(),
            ),
        )


def load_chat_state(project_id: int, user_email: str = "") -> tuple[list, list]:
    conn = _get_connection()
    row = conn.execute(
        "SELECT messages_json, claude_history_json FROM chat_history_v2 "
        "WHERE project_id = ? AND user_email = ?",
        (project_id, (user_email or "").lower().strip()),
    ).fetchone()
    if not row:
        row = conn.execute(
            "SELECT messages_json, claude_history_json FROM chat_history "
            "WHERE project_id = ?",
            (project_id,),
        ).fetchone()
    conn.close()
    if not row:
        return [], []
    try:
        return (
            json.loads(row["messages_json"] or "[]"),
            json.loads(row["claude_history_json"] or "[]"),
        )
    except (json.JSONDecodeError, TypeError):
        return [], []


def clear_chat_state(project_id: int, user_email: str | None = None) -> None:
    with _db_cursor() as cursor:
        if user_email is None:
            cursor.execute(
                "DELETE FROM chat_history_v2 WHERE project_id = ?", (project_id,)
            )
        else:
            cursor.execute(
                "DELETE FROM chat_history_v2 WHERE project_id = ? AND user_email = ?",
                (project_id, (user_email or "").lower().strip()),
            )
        cursor.execute("DELETE FROM chat_history WHERE project_id = ?", (project_id,))


def set_org_llm_key(provider: str, key: str, updated_by: str = "") -> None:
    from auth import crypto_store

    enc = crypto_store.encrypt(key)
    with _db_cursor() as cursor:
        cursor.execute(
            "INSERT INTO org_llm_keys (provider, key_encrypted, updated_at, updated_by) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(provider) DO UPDATE SET "
            "key_encrypted = excluded.key_encrypted, updated_at = excluded.updated_at, "
            "updated_by = excluded.updated_by",
            (provider.lower().strip(), enc, datetime.now().isoformat(), updated_by),
        )


def get_org_llm_key(provider: str) -> str | None:
    conn = _get_connection()
    row = conn.execute(
        "SELECT key_encrypted FROM org_llm_keys WHERE provider = ?",
        (provider.lower().strip(),),
    ).fetchone()
    conn.close()
    if not row:
        return None
    try:
        from auth import crypto_store

        return crypto_store.decrypt(row["key_encrypted"])
    except Exception:
        return None


def clear_org_llm_key(provider: str) -> None:
    with _db_cursor() as cursor:
        cursor.execute(
            "DELETE FROM org_llm_keys WHERE provider = ?", (provider.lower().strip(),)
        )


def org_llm_key_status() -> dict[str, dict[str, Any]]:
    conn = _get_connection()
    rows = conn.execute(
        "SELECT provider, updated_at, updated_by FROM org_llm_keys"
    ).fetchall()
    conn.close()
    return {
        r["provider"]: {
            "set": True,
            "updated_at": r["updated_at"],
            "updated_by": r["updated_by"],
        }
        for r in rows
    }


def set_fabric_connection(
    tenant_id: str,
    client_id: str,
    client_secret: str,
    workspace_name: str,
    workspace_id: str,
    models: list,
    catalogs: dict,
    updated_by: str = "",
) -> None:
    from auth import crypto_store

    enc = crypto_store.encrypt(client_secret)
    with _db_cursor() as cursor:
        cursor.execute(
            "INSERT INTO fabric_connection "
            "(id, tenant_id, client_id, secret_encrypted, workspace_name, workspace_id, "
            " models_json, catalogs_json, updated_at, updated_by) "
            "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "tenant_id=excluded.tenant_id, client_id=excluded.client_id, "
            "secret_encrypted=excluded.secret_encrypted, "
            "workspace_name=excluded.workspace_name, workspace_id=excluded.workspace_id, "
            "models_json=excluded.models_json, catalogs_json=excluded.catalogs_json, "
            "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (
                tenant_id.strip(),
                client_id.strip(),
                enc,
                workspace_name.strip(),
                workspace_id.strip(),
                json.dumps(list(models)),
                json.dumps(dict(catalogs)),
                datetime.now().isoformat(),
                updated_by,
            ),
        )


def get_fabric_connection_row() -> dict[str, Any] | None:
    conn = _get_connection()
    row = conn.execute(
        "SELECT tenant_id, client_id, secret_encrypted, workspace_name, workspace_id, "
        "models_json, catalogs_json, updated_at, updated_by "
        "FROM fabric_connection WHERE id = 1"
    ).fetchone()
    conn.close()
    if not row:
        return None
    try:
        from auth import crypto_store

        secret = crypto_store.decrypt(row["secret_encrypted"])
    except Exception:
        return None
    return {
        "tenant_id": row["tenant_id"],
        "client_id": row["client_id"],
        "client_secret": secret,
        "workspace_name": row["workspace_name"],
        "workspace_id": row["workspace_id"],
        "models": json.loads(row["models_json"] or "[]"),
        "catalogs": json.loads(row["catalogs_json"] or "{}"),
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
    }


def clear_fabric_connection() -> None:
    with _db_cursor() as cursor:
        cursor.execute("DELETE FROM fabric_connection WHERE id = 1")


# ---------------------------------------------------------------------------
# Fabric connection LIBRARY (many accounts) + per-project binding
# ---------------------------------------------------------------------------


# A client ID is a GUID and is not a secret; a client secret is neither. The
# two sit next to each other on the connection form, and one of them has been
# typed into the other's box -- which put a live secret in a column every
# listing is free to show. Anything here that is not GUID-shaped is therefore
# treated as "not a client ID" and withheld from callers that asked for no
# secrets, because that is the only safe reading of a value in the wrong field.
_GUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)


def _row_to_fabric_conn(row, *, with_secret: bool = True) -> dict[str, Any] | None:
    if not row:
        return None
    client_id = row["client_id"] or ""
    malformed = bool(client_id) and not _GUID_RE.match(client_id)
    out: dict[str, Any] = {
        "id": row["id"],
        "name": row["name"],
        "tenant_id": row["tenant_id"],
        "client_id": "" if (malformed and not with_secret) else client_id,
        "client_id_malformed": malformed,
        "workspace_name": row["workspace_name"],
        "workspace_id": row["workspace_id"],
        "models": json.loads(row["models_json"] or "[]"),
        "catalogs": json.loads(row["catalogs_json"] or "{}"),
        "is_default": bool(row["is_default"]),
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
    }
    if with_secret:
        from auth import crypto_store

        try:
            out["client_secret"] = crypto_store.decrypt(row["secret_encrypted"])
        except Exception:
            return None
    return out


def list_fabric_connections() -> list[dict[str, Any]]:
    """Every saved connection, secrets omitted (safe for UI listings)."""
    conn = _get_connection()
    rows = conn.execute(
        "SELECT * FROM fabric_connections ORDER BY is_default DESC, name COLLATE NOCASE"
    ).fetchall()
    conn.close()
    return [r for r in (_row_to_fabric_conn(x, with_secret=False) for x in rows) if r]


def get_fabric_connection_by_id(conn_id: int) -> dict[str, Any] | None:
    conn = _get_connection()
    row = conn.execute(
        "SELECT * FROM fabric_connections WHERE id = ?", (conn_id,)
    ).fetchone()
    conn.close()
    return _row_to_fabric_conn(row)


def get_default_fabric_connection() -> dict[str, Any] | None:
    conn = _get_connection()
    row = conn.execute(
        "SELECT * FROM fabric_connections ORDER BY is_default DESC, id LIMIT 1"
    ).fetchone()
    conn.close()
    return _row_to_fabric_conn(row)


def resolve_fabric_connection(
    project_id: int | None = None,
) -> dict[str, Any] | None:
    """The connection a project should use: its binding, else the default."""
    if project_id:
        conn = _get_connection()
        row = conn.execute(
            "SELECT fabric_connection_id FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        conn.close()
        bound = row["fabric_connection_id"] if row else None
        if bound:
            got = get_fabric_connection_by_id(bound)
            if got:
                return got
    return get_default_fabric_connection()


def save_fabric_connection_entry(
    name: str,
    tenant_id: str,
    client_id: str,
    client_secret: str,
    workspace_name: str,
    workspace_id: str,
    models: list,
    catalogs: dict,
    conn_id: int | None = None,
    make_default: bool = False,
    updated_by: str = "",
) -> int:
    """Create or update one connection in the library. Returns its id.

    An empty `client_secret` on update keeps the stored secret, so the UI can
    show a blank secret box without wiping the credential.
    """
    from auth import crypto_store

    with _db_cursor() as cursor:
        enc = ""
        if client_secret:
            enc = crypto_store.encrypt(client_secret)
        elif conn_id:
            prev = cursor.execute(
                "SELECT secret_encrypted FROM fabric_connections WHERE id = ?",
                (conn_id,),
            ).fetchone()
            enc = prev["secret_encrypted"] if prev else ""
        if not enc:
            raise ValueError("A client secret is required for a new connection.")

        now = datetime.now().isoformat()
        if conn_id:
            cursor.execute(
                "UPDATE fabric_connections SET name=?, tenant_id=?, client_id=?, "
                "secret_encrypted=?, workspace_name=?, workspace_id=?, models_json=?, "
                "catalogs_json=?, updated_at=?, updated_by=? WHERE id=?",
                (
                    name.strip(),
                    tenant_id.strip(),
                    client_id.strip(),
                    enc,
                    workspace_name.strip(),
                    workspace_id.strip(),
                    json.dumps(list(models)),
                    json.dumps(dict(catalogs)),
                    now,
                    updated_by,
                    conn_id,
                ),
            )
            new_id = conn_id
        else:
            cursor.execute(
                "INSERT INTO fabric_connections "
                "(name, tenant_id, client_id, secret_encrypted, workspace_name, "
                " workspace_id, models_json, catalogs_json, is_default, updated_at, "
                " updated_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (
                    name.strip(),
                    tenant_id.strip(),
                    client_id.strip(),
                    enc,
                    workspace_name.strip(),
                    workspace_id.strip(),
                    json.dumps(list(models)),
                    json.dumps(dict(catalogs)),
                    now,
                    updated_by,
                ),
            )
            new_id = cursor.lastrowid
            # First connection saved is automatically the default.
            n = cursor.execute(
                "SELECT COUNT(*) AS n FROM fabric_connections"
            ).fetchone()
            if (n["n"] if n else 0) == 1:
                make_default = True

        if make_default:
            cursor.execute("UPDATE fabric_connections SET is_default = 0")
            cursor.execute(
                "UPDATE fabric_connections SET is_default = 1 WHERE id = ?", (new_id,)
            )
        return new_id


def set_default_fabric_connection(conn_id: int) -> None:
    with _db_cursor() as cursor:
        cursor.execute("UPDATE fabric_connections SET is_default = 0")
        cursor.execute(
            "UPDATE fabric_connections SET is_default = 1 WHERE id = ?", (conn_id,)
        )


def delete_fabric_connection_entry(conn_id: int) -> None:
    with _db_cursor() as cursor:
        cursor.execute("DELETE FROM fabric_connections WHERE id = ?", (conn_id,))
        # Projects pointing at it fall back to the default.
        cursor.execute(
            "UPDATE projects SET fabric_connection_id = NULL "
            "WHERE fabric_connection_id = ?",
            (conn_id,),
        )
        left = cursor.execute(
            "SELECT id FROM fabric_connections WHERE is_default = 1"
        ).fetchone()
        if not left:
            nxt = cursor.execute(
                "SELECT id FROM fabric_connections ORDER BY id LIMIT 1"
            ).fetchone()
            if nxt:
                cursor.execute(
                    "UPDATE fabric_connections SET is_default = 1 WHERE id = ?",
                    (nxt["id"],),
                )


def set_project_fabric_connection(project_id: int, conn_id: int | None) -> None:
    """Bind a project to a connection; None means 'use the default'."""
    with _db_cursor() as cursor:
        cursor.execute(
            "UPDATE projects SET fabric_connection_id = ? WHERE id = ?",
            (conn_id, project_id),
        )


def get_project_fabric_connection_id(project_id: int) -> int | None:
    conn = _get_connection()
    row = conn.execute(
        "SELECT fabric_connection_id FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    conn.close()
    return row["fabric_connection_id"] if row else None


def fabric_connection_status() -> dict[str, Any]:
    conn = _get_connection()
    row = conn.execute(
        "SELECT workspace_name, models_json, updated_at, updated_by "
        "FROM fabric_connection WHERE id = 1"
    ).fetchone()
    conn.close()
    if not row:
        return {"set": False}
    return {
        "set": True,
        "workspace_name": row["workspace_name"],
        "models": json.loads(row["models_json"] or "[]"),
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
    }


ROLES = ("admin", "editor", "viewer")


def create_app_user(email: str, password_hash: str, role: str = "editor") -> None:
    if role not in ROLES:
        raise ValueError(f"Invalid role '{role}'. Must be one of: {', '.join(ROLES)}")
    try:
        with _db_cursor() as cursor:
            cursor.execute(
                "INSERT INTO app_users (email, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                (
                    email.lower().strip(),
                    password_hash,
                    role,
                    datetime.now().isoformat(),
                ),
            )
    except sqlite3.IntegrityError:
        raise ValueError(f"User '{email}' already exists.")


def save_user_workspace_access(email: str, workspaces: list[dict[str, Any]]) -> None:
    """Replace a user's cached workspace list with what Power BI just returned.

    Replace, never merge: a workspace the user has lost access to must vanish,
    and a merge would keep it forever.
    """
    email = (email or "").lower().strip()
    now = datetime.now().isoformat()
    conn = _get_connection()
    try:
        conn.execute("DELETE FROM user_workspace_access WHERE user_email = ?", (email,))
        for w in workspaces:
            conn.execute(
                "INSERT INTO user_workspace_access "
                "(user_email, workspace_id, workspace_name, is_read_only, checked_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    email,
                    (w.get("id") or "").strip(),
                    w.get("name", ""),
                    1 if w.get("isReadOnly") else 0,
                    now,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def get_user_workspace_access(email: str) -> dict[str, dict[str, Any]]:
    """Cached {workspace_id: {name, is_read_only, checked_at}} for a user."""
    email = (email or "").lower().strip()
    conn = _get_connection()
    rows = conn.execute(
        "SELECT workspace_id, workspace_name, is_read_only, checked_at "
        "FROM user_workspace_access WHERE user_email = ?",
        (email,),
    ).fetchall()
    conn.close()
    return {
        r["workspace_id"]: {
            "name": r["workspace_name"],
            "is_read_only": bool(r["is_read_only"]),
            "checked_at": r["checked_at"],
        }
        for r in rows
    }


def clear_user_workspace_access(email: str) -> None:
    email = (email or "").lower().strip()
    conn = _get_connection()
    conn.execute("DELETE FROM user_workspace_access WHERE user_email = ?", (email,))
    conn.commit()
    conn.close()


def save_user_token_cache(email: str, blob: str) -> None:
    """Persist the user's MSAL cache, encrypted. Silent refresh depends on it."""
    from auth import crypto_store

    email = (email or "").lower().strip()
    conn = _get_connection()
    conn.execute(
        "INSERT INTO user_token_cache (user_email, cache_encrypted, updated_at) "
        "VALUES (?, ?, ?) ON CONFLICT(user_email) DO UPDATE SET "
        "cache_encrypted = excluded.cache_encrypted, updated_at = excluded.updated_at",
        (email, crypto_store.encrypt(blob), datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def load_user_token_cache(email: str) -> str:
    from auth import crypto_store

    email = (email or "").lower().strip()
    conn = _get_connection()
    row = conn.execute(
        "SELECT cache_encrypted FROM user_token_cache WHERE user_email = ?", (email,)
    ).fetchone()
    conn.close()
    if not row:
        return ""
    try:
        return crypto_store.decrypt(row["cache_encrypted"])
    except Exception:
        return ""


def clear_user_token_cache(email: str) -> None:
    email = (email or "").lower().strip()
    conn = _get_connection()
    conn.execute("DELETE FROM user_token_cache WHERE user_email = ?", (email,))
    conn.commit()
    conn.close()


def get_app_user(email: str) -> dict[str, Any] | None:
    conn = _get_connection()
    row = conn.execute(
        "SELECT email, password_hash, role, is_active FROM app_users WHERE email = ?",
        (email.lower().strip(),),
    ).fetchone()
    conn.close()
    return (
        {
            "email": row["email"],
            "password_hash": row["password_hash"],
            "role": row["role"],
            "is_active": bool(row["is_active"]),
        }
        if row
        else None
    )


def list_app_users() -> list[dict[str, Any]]:
    conn = _get_connection()
    rows = conn.execute(
        "SELECT email, role, is_active, created_at FROM app_users ORDER BY created_at"
    ).fetchall()
    conn.close()
    return [
        {
            "email": r["email"],
            "role": r["role"],
            "is_active": bool(r["is_active"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def set_user_active(email: str, active: bool) -> None:
    with _db_cursor() as cursor:
        cursor.execute(
            "UPDATE app_users SET is_active = ? WHERE email = ?",
            (1 if active else 0, email.lower().strip()),
        )


def count_active_admins() -> int:
    conn = _get_connection()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM app_users WHERE role = 'admin' AND is_active = 1"
    ).fetchone()
    conn.close()
    return int(row["n"]) if row else 0


def update_user_role(email: str, role: str) -> None:
    if role not in ROLES:
        raise ValueError(f"Invalid role '{role}'.")
    with _db_cursor() as cursor:
        cursor.execute(
            "UPDATE app_users SET role = ? WHERE email = ?",
            (role, email.lower().strip()),
        )


def delete_app_user(email: str) -> None:
    with _db_cursor() as cursor:
        cursor.execute(
            "DELETE FROM app_users WHERE email = ?", (email.lower().strip(),)
        )


def audit(
    event_type: str,
    target: str = "",
    details: str = "",
    user_email: str = "",
    severity: str = "info",
    outcome: str = "success",
    ip_address: str = "",
    project_id: int | None = None,
    project_name: str = "",
) -> None:
    try:
        from db.audit_context import get_context

        _ctx = get_context()
    except Exception:
        _ctx = {}
    user_email = user_email or _ctx.get("user_email", "")
    ip_address = ip_address or _ctx.get("ip_address", "")
    if project_id is None:
        project_id = _ctx.get("project_id")
    project_name = project_name or _ctx.get("project_name", "")
    try:
        conn = _get_connection()
        try:
            conn.execute(
                "INSERT INTO audit_log "
                "(created_at, user_email, event_type, target, details, severity, outcome, "
                " ip_address, project_id, project_name) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    datetime.utcnow().isoformat(),
                    user_email or "",
                    event_type,
                    target or "",
                    details or "",
                    severity or "info",
                    outcome or "success",
                    ip_address or "",
                    project_id,
                    project_name or "",
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def list_audit_log(
    limit: int = 500,
    user_email: str = "",
    event_type: str = "",
    severity: str = "",
    project_name: str = "",
    since: str = "",
) -> list[dict[str, Any]]:
    conn = _get_connection()
    query = "SELECT * FROM audit_log WHERE 1=1"
    params: list = []
    if user_email:
        query += " AND user_email = ?"
        params.append(user_email)
    if event_type:
        query += " AND event_type = ?"
        params.append(event_type)
    if severity:
        query += " AND severity = ?"
        params.append(severity)
    if project_name:
        query += " AND project_name = ?"
        params.append(project_name)
    if since:
        query += " AND created_at >= ?"
        params.append(since)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def distinct_audit_users() -> list[str]:
    conn = _get_connection()
    rows = conn.execute(
        "SELECT DISTINCT user_email FROM audit_log "
        "WHERE user_email != '' ORDER BY user_email"
    ).fetchall()
    conn.close()
    return [r["user_email"] for r in rows]


def distinct_audit_event_types() -> list[str]:
    conn = _get_connection()
    rows = conn.execute(
        "SELECT DISTINCT event_type FROM audit_log ORDER BY event_type"
    ).fetchall()
    conn.close()
    return [r["event_type"] for r in rows]


def distinct_audit_projects() -> list[str]:
    conn = _get_connection()
    rows = conn.execute(
        "SELECT DISTINCT project_name FROM audit_log "
        "WHERE project_name != '' ORDER BY project_name"
    ).fetchall()
    conn.close()
    return [r["project_name"] for r in rows]


def create_client(name: str, description: str = "", metadata: dict | None = None) -> Client:
    meta_json = json.dumps(metadata or {})
    now = datetime.now().isoformat()

    try:
        with _db_cursor() as cursor:
            cursor.execute(
                "INSERT INTO clients (name, description, metadata, created_at) VALUES (?, ?, ?, ?)",
                (name, description, meta_json, now),
            )
            client_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        raise ValueError(f"Client '{name}' already exists")

    return Client(
        id=client_id, name=name, description=description, metadata=metadata or {}
    )


def list_clients() -> list[Client]:
    conn = _get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM clients ORDER BY name")
    rows = cursor.fetchall()
    conn.close()

    return [
        Client(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            metadata=json.loads(row["metadata"] or "{}"),
        )
        for row in rows
    ]


def create_project(
    client_id: int,
    name: str,
    description: str = "",
    metadata: dict | None = None,
    user_email: str = "",
) -> Project:
    import re

    from config import PROJECTS_DIR

    meta_json = json.dumps(metadata or {})
    now = datetime.now().isoformat()

    try:
        with _db_cursor() as cursor:
            cursor.execute(
                "INSERT INTO projects (client_id, name, description, metadata, created_at) VALUES (?, ?, ?, ?, ?)",
                (client_id, name, description, meta_json, now),
            )
            project_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        raise ValueError(f"Project '{name}' already exists for this client")

    slug = re.sub(r"[^\w\-]", "_", name).strip("_") or "project"
    proj_root = PROJECTS_DIR / slug
    (proj_root / "model").mkdir(parents=True, exist_ok=True)
    (proj_root / "report").mkdir(parents=True, exist_ok=True)

    # The only audit for a create — callers must not log a second one, or the
    # log shows every project being created twice.
    audit(
        "project.create",
        target=name,
        details=description,
        user_email=user_email,
        project_id=project_id,
        project_name=name,
    )

    return Project(
        id=project_id,
        client_id=client_id,
        name=name,
        description=description,
        metadata=metadata or {},
        created_at=now,
    )


def get_project(project_id: int) -> Project | None:
    conn = _get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return None

    return Project(
        id=row["id"],
        client_id=row["client_id"],
        name=row["name"],
        description=row["description"],
        metadata=json.loads(row["metadata"] or "{}"),
        created_at=row["created_at"],
    )


def update_project(
    project_id: int, name: str, description: str = "", user_email: str = ""
) -> Project:
    """Rename a project / rewrite its description.

    Only the two editable columns are touched — `metadata` carries the sources,
    requirements, and LLM config, and is written through
    _update_project_metadata's compare-and-swap. Writing it from here as well
    would let a rename clobber a concurrent source connection.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("A project name is required.")

    before = get_project(project_id)
    if not before:
        raise ValueError(f"Project {project_id} not found")

    try:
        with _db_cursor() as cursor:
            cursor.execute(
                "UPDATE projects SET name = ?, description = ? WHERE id = ?",
                (name, description or "", project_id),
            )
    except sqlite3.IntegrityError:
        raise ValueError(f"Another project is already called '{name}'.")

    try:
        detail = (
            f"renamed '{before.name}' -> '{name}'"
            if before.name != name
            else "description updated"
        )
        audit(
            "project.update",
            target=name,
            details=detail,
            user_email=user_email,
            project_id=project_id,
            project_name=name,
        )
    except Exception:
        pass  # never lose a good write to an audit failure

    before.name = name
    before.description = description or ""
    return before


# Child tables deleted with project; audit_log is intentionally excluded
_PROJECT_CHILD_TABLES = (
    "documents",
    "wireframes",
    "semantic_models",
    "api_usage",
    "project_rules",
    "project_members",
    "project_skills",
    "chat_history",
    "chat_history_v2",
)


def delete_project(project_id: int, user_email: str = "") -> str:
    """Delete a project and everything hanging off it. Returns the name deleted.

    The child rows go in one transaction so a failure part-way cannot leave
    documents or wireframes orphaned against a project id that no longer
    resolves. Uploaded files are removed afterwards, outside the transaction —
    a file that will not delete (locked on Windows, already gone) must not roll
    back a committed delete.
    """
    proj = get_project(project_id)
    if not proj:
        raise ValueError(f"Project {project_id} not found")

    doc_paths = [d.file_path for d in list_documents(project_id) if d.file_path]

    conn = _get_connection()
    try:
        cursor = conn.cursor()
        for table in _PROJECT_CHILD_TABLES:
            try:
                cursor.execute(
                    f"DELETE FROM {table} WHERE project_id = ?", (project_id,)
                )
            except sqlite3.OperationalError:
                continue  # table not present in this database — nothing to unlink
        cursor.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        conn.commit()
    finally:
        conn.close()

    for path in doc_paths:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass  # the row is already gone; a stranded file is not worth failing on
    try:
        upload_dir = os.path.join(UPLOADS_DIR, str(project_id))
        if os.path.isdir(upload_dir):
            import shutil

            shutil.rmtree(upload_dir, ignore_errors=True)
    except OSError:
        pass

    try:
        audit(
            "project.delete",
            target=proj.name,
            details=f"{len(doc_paths)} document(s) removed",
            severity="warning",
            user_email=user_email,
            project_id=project_id,
            project_name=proj.name,
        )
    except Exception:
        pass
    return proj.name


def get_or_create_default_client() -> "Client":
    clients = list_clients()
    if clients:
        return clients[0]
    return create_client("Default", "Auto-created")


def get_project_requirements(project_id: int) -> str:
    p = get_project(project_id)
    if not p:
        return ""
    return ((p.metadata or {}).get("requirements") or "").strip()


def _update_project_metadata(project_id: int, mutate) -> str:
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        for _attempt in range(5):
            cursor.execute(
                "SELECT metadata, name FROM projects WHERE id = ?", (project_id,)
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError(f"Project {project_id} not found")
            raw = row["metadata"]
            try:
                meta = json.loads(raw) if raw else {}
            except (json.JSONDecodeError, TypeError):
                meta = {}
            meta = mutate(meta)
            cursor.execute(
                "UPDATE projects SET metadata = ? WHERE id = ? AND metadata IS ?",
                (json.dumps(meta), project_id, raw),
            )
            if cursor.rowcount:
                conn.commit()
                return row["name"]
        raise RuntimeError(
            f"metadata update on project {project_id} lost the concurrency race 5 times"
        )
    finally:
        conn.close()


class StaleWriteError(RuntimeError):
    """A write was refused because the stored value moved on since it was read."""


def requirements_revision(text: str) -> str:
    """An opaque token for a spec's content — equal texts give equal tokens.

    Derived from the text rather than a stored counter so it needs no migration
    and cannot drift out of step with what is actually saved.
    """
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()[:16]


def set_project_requirements(
    project_id: int, requirements: str, *, expected_rev: str | None = None
) -> str:
    """Save the spec and return its new revision.

    _update_project_metadata's compare-and-swap keeps concurrent writers from
    clobbering each *other's keys*, but on retry it re-applies this mutate with
    the same `requirements` — so without expected_rev the last writer silently
    wins the field. Pass the revision the text was read at to be refused instead.
    """

    def _mutate(meta: dict) -> dict:
        if expected_rev is not None:
            stored = (meta.get("requirements") or "").strip()
            if requirements_revision(stored) != expected_rev:
                raise StaleWriteError(
                    "The saved requirements changed since this page loaded."
                )
        meta["requirements"] = requirements
        return meta

    _update_project_metadata(project_id, _mutate)
    return requirements_revision(requirements)


_SOURCE_SECRET_FIELDS = ("password",)


def set_project_source(
    project_id: int, kind: str, config: dict[str, str], user_email: str = ""
) -> None:
    """Persist a source config, encrypting its secrets.

    A secret field *absent* from `config` keeps whatever is already stored. The
    form never receives the saved password back, so "left blank" has to mean
    "unchanged" rather than "wipe it".
    """
    from auth import crypto_store

    plain = {k: v for k, v in config.items() if k not in _SOURCE_SECRET_FIELDS}

    def _mutate(meta: dict) -> dict:
        existing = (meta.get("sources") or {}).get(kind) or {}
        stored = dict(plain)
        for field_name in _SOURCE_SECRET_FIELDS:
            key = f"{field_name}_encrypted"
            if field_name not in config:
                stored[key] = existing.get(key, "")
                continue
            secret = (config.get(field_name) or "").strip()
            stored[key] = crypto_store.encrypt(secret) if secret else ""
        meta.setdefault("sources", {})[kind] = stored
        return meta

    name = _update_project_metadata(project_id, _mutate)
    try:
        audit(
            f"project.source.{kind}.set",
            target=name,
            details=f"{config.get('server', '')}/{config.get('database', '')}",
            user_email=user_email,
            project_id=project_id,
        )
    except Exception:
        pass


def get_project_source(
    project_id: int, kind: str, *, include_secrets: bool = False
) -> dict[str, str] | None:
    """The stored config for a source.

    Secrets are withheld unless asked for: the page renders this into a form, and
    a decrypted password placed in a widget's value travels to the browser and
    sits in the DOM on every rerun. Callers that actually open a connection pass
    include_secrets=True at the point of use instead. Without it, each secret
    field is reported as a bool under "<field>_set" so the form can say whether
    one is saved without ever holding it.
    """
    p = get_project(project_id)
    if not p or not p.metadata:
        return None
    cfg = (p.metadata.get("sources") or {}).get(kind)
    if not isinstance(cfg, dict) or not cfg.get("server"):
        return None
    out = {k: v for k, v in cfg.items() if not k.endswith("_encrypted")}

    for field_name in _SOURCE_SECRET_FIELDS:
        token = cfg.get(f"{field_name}_encrypted") or ""
        out[f"{field_name}_set"] = bool(token)
        if not include_secrets:
            continue
        if not token:
            out[field_name] = ""
            continue
        from auth import crypto_store

        try:
            out[field_name] = crypto_store.decrypt(token)
        except Exception:
            # Master key rotated or lost — treat as "no credential", never crash.
            out[field_name] = ""
    return out


# Context-block key suffix holding the structured [{name, columns}] catalog for
# a source, used to offer real column choices when binding KPIs.
COLUMN_CATALOG_SUFFIX = "__columns"


def clear_project_source(project_id: int, kind: str, user_email: str = "") -> None:
    def _mutate(meta: dict) -> dict:
        (meta.get("sources") or {}).pop(kind, None)
        meta.get("context_blocks", {}).pop(kind, None)
        # Companion catalog written alongside the block at connect time —
        # drop it together or the column picker offers a disconnected source.
        meta.get("context_blocks", {}).pop(f"{kind}{COLUMN_CATALOG_SUFFIX}", None)
        return meta

    name = _update_project_metadata(project_id, _mutate)
    try:
        audit(
            f"project.source.{kind}.clear",
            target=name,
            user_email=user_email,
            project_id=project_id,
        )
    except Exception:
        pass


def set_project_context_block(project_id: int, kind: str, text: str) -> None:
    def _mutate(meta: dict) -> dict:
        meta.setdefault("context_blocks", {})[kind] = text
        return meta

    _update_project_metadata(project_id, _mutate)


def get_project_context_block(project_id: int, kind: str) -> str:
    p = get_project(project_id)
    if not p or not p.metadata:
        return ""
    return ((p.metadata.get("context_blocks") or {}).get(kind) or "").strip()


def get_project_llm_config(project_id: int) -> dict[str, str] | None:
    p = get_project(project_id)
    if not p or not p.metadata:
        return None
    cfg = p.metadata.get("llm_config")
    if isinstance(cfg, dict) and cfg.get("provider") and cfg.get("model"):
        return {
            "provider": str(cfg["provider"]).strip().lower(),
            "model": str(cfg["model"]).strip(),
        }
    return None


def set_project_llm_config(
    project_id: int, provider: str, model: str, user_email: str = ""
) -> None:
    prov_clean = provider.strip().lower()
    mod_clean = model.strip()

    def _mutate(meta: dict) -> dict:
        meta["llm_config"] = {"provider": prov_clean, "model": mod_clean}
        return meta

    name = _update_project_metadata(project_id, _mutate)
    try:
        audit(
            "project.llm_config.set",
            target=name,
            details=f"{prov_clean}/{mod_clean}",
            user_email=user_email,
            project_id=project_id,
        )
    except Exception:
        pass


def clear_project_llm_config(project_id: int, user_email: str = "") -> None:

    def _mutate(meta: dict) -> dict:
        meta.pop("llm_config", None)
        return meta

    name = _update_project_metadata(project_id, _mutate)
    try:
        audit(
            "project.llm_config.clear",
            target=name,
            user_email=user_email,
            project_id=project_id,
        )
    except Exception:
        pass


def get_project_memory(project_id: int) -> str:
    p = get_project(project_id)
    if not p:
        return ""
    meta = p.metadata or {}
    return (meta.get("fabricbi_memory") or "").strip()


def append_project_memory(project_id: int, note: str) -> None:
    note = (note or "").strip()
    if not note:
        return
    from config import FABRICBI_MEMORY_MAX_CHARS

    def _mutate(meta: dict) -> dict:
        existing = (meta.get("fabricbi_memory") or "").strip()
        combined = (existing + "\n" + note).strip() if existing else note
        if len(combined) > FABRICBI_MEMORY_MAX_CHARS:
            combined = combined[-FABRICBI_MEMORY_MAX_CHARS:]
            combined = combined.split("\n", 1)[-1] if "\n" in combined else combined
        meta["fabricbi_memory"] = combined
        return meta

    try:
        _update_project_metadata(project_id, _mutate)
    except ValueError:
        pass  # unknown project — no-op, matching prior behavior


def _list_all_projects() -> list[Project]:
    """Every project in the table, unfiltered.

    Private on purpose: no membership check and no Fabric gate. Anything that
    puts projects in front of a user goes through list_projects_for_user.
    """
    conn = _get_connection()
    rows = conn.execute("SELECT * FROM projects ORDER BY name").fetchall()
    conn.close()
    return [
        Project(
            id=r["id"],
            client_id=r["client_id"],
            name=r["name"],
            description=r["description"],
            metadata=json.loads(r["metadata"] or "{}"),
            created_at=r["created_at"],
        )
        for r in rows
    ]


def find_project_index(projects: list[Project], active_id) -> int:
    return next((i for i, p in enumerate(projects) if p.id == active_id), 0)


def find_project_by_id(projects: list[Project], active_id) -> Project | None:
    return next((p for p in projects if p.id == active_id), None)


def project_workspace_ids() -> dict[int, str]:
    """{project_id: workspace_id} for every project bound to a connection.

    A project with no binding is absent, not empty-string — the caller has to
    decide what an unbound project means rather than having it silently match
    a blank workspace id.
    """
    conn = _get_connection()
    rows = conn.execute(
        "SELECT p.id AS pid, fc.workspace_id AS wsid FROM projects p "
        "JOIN fabric_connections fc ON fc.id = p.fabric_connection_id"
    ).fetchall()
    conn.close()
    return {
        r["pid"]: (r["wsid"] or "").strip() for r in rows if (r["wsid"] or "").strip()
    }


def _apply_fabric_gate(projects: list[Project], user_email: str) -> list[Project]:
    """Drop projects whose Fabric workspace this user cannot see.

    Applies to admins too. A FabricBI admin who is not in the workspace has no
    business opening a project bound to it — the admin role governs FabricBI's own
    settings, not somebody else's Fabric data. Admin-only pages are not
    project-scoped and are unaffected.

    An unbound project (no Fabric connection) is left visible: there is no
    workspace to check it against, and project_members still applies.
    """
    from auth.workspace_access import for_user

    access = for_user(user_email)
    if access.status == "off":
        return projects
    if not access.usable:
        return []  # fail closed
    ws_by_project = project_workspace_ids()
    return [
        p
        for p in projects
        if p.id not in ws_by_project or access.can_see(ws_by_project[p.id])
    ]


def list_projects_for_user(user_email: str, is_admin: bool) -> list[Project]:
    if is_admin:
        return _apply_fabric_gate(_list_all_projects(), user_email)
    conn = _get_connection()
    rows = conn.execute(
        """
        SELECT p.* FROM projects p
        WHERE EXISTS (
                SELECT 1 FROM project_members pm
                WHERE pm.project_id = p.id AND LOWER(pm.user_email) = ?
           )
        ORDER BY p.name
        """,
        ((user_email or "").lower().strip(),),
    ).fetchall()
    conn.close()
    return _apply_fabric_gate(
        [
            Project(
                id=r["id"],
                client_id=r["client_id"],
                name=r["name"],
                description=r["description"],
                metadata=json.loads(r["metadata"] or "{}"),
                created_at=r["created_at"],
            )
            for r in rows
        ],
        user_email,
    )


def list_projects(client_id: int) -> list[Project]:
    conn = _get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT * FROM projects WHERE client_id = ? ORDER BY created_at DESC",
        (client_id,),
    )
    rows = cursor.fetchall()
    conn.close()

    return [
        Project(
            id=row["id"],
            client_id=row["client_id"],
            name=row["name"],
            description=row["description"],
            metadata=json.loads(row["metadata"] or "{}"),
            created_at=row["created_at"],
        )
        for row in rows
    ]


def save_wireframe(
    project_id: int,
    title: str,
    description: str = "",
    spec: dict | None = None,
    image: str = "",
    status: str = "draft",
) -> Wireframe:
    spec_json = json.dumps(spec or {})
    now = datetime.now().isoformat()

    with _db_cursor() as cursor:
        cursor.execute(
            """INSERT INTO wireframes (project_id, title, description, spec, image, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (project_id, title, description, spec_json, image, status, now, now),
        )
        wf_id = cursor.lastrowid

    return Wireframe(
        id=wf_id,
        project_id=project_id,
        title=title,
        description=description,
        spec=spec or {},
        image=image,
        status=status,
        created_at=now,
        updated_at=now,
    )


def list_wireframes(project_id: int) -> list[Wireframe]:
    conn = _get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT * FROM wireframes WHERE project_id = ? ORDER BY created_at DESC",
        (project_id,),
    )
    rows = cursor.fetchall()
    conn.close()

    return [
        Wireframe(
            id=row["id"],
            project_id=row["project_id"],
            title=row["title"],
            description=row["description"],
            spec=json.loads(row["spec"] or "{}"),
            image=row["image"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        for row in rows
    ]


def get_wireframe(wireframe_id: int) -> Wireframe | None:
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM wireframes WHERE id = ?",
        (wireframe_id,),
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return Wireframe(
        id=row["id"],
        project_id=row["project_id"],
        title=row["title"],
        description=row["description"],
        spec=json.loads(row["spec"] or "{}"),
        image=row["image"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def update_wireframe_status(
    wireframe_id: int, status: str, *, project_id: int
) -> bool:
    now = datetime.now().isoformat()
    with _db_cursor() as cursor:
        cursor.execute(
            "UPDATE wireframes SET status = ?, updated_at = ? WHERE id = ? AND project_id = ?",
            (status, now, wireframe_id, project_id),
        )
        return cursor.rowcount > 0


def get_project_upload_dir(project_id: int) -> str:
    path = os.path.join(UPLOADS_DIR, str(project_id))
    os.makedirs(path, exist_ok=True)
    return path


def _safe_upload_filename(original_name: str) -> str:
    base = os.path.basename(original_name.replace("\\", "/"))
    base = base.replace("..", "").replace(" ", "_").strip("._")
    return base or "upload"


def save_document(
    project_id: int,
    original_name: str,
    file_bytes: bytes,
    file_type: str = "",
    description: str = "",
) -> Document:
    upload_dir = get_project_upload_dir(project_id)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = _safe_upload_filename(original_name)
    filename = f"{timestamp}_{safe_name}"
    file_path = os.path.join(upload_dir, filename)

    if os.path.commonpath(
        [os.path.realpath(file_path), os.path.realpath(upload_dir)]
    ) != os.path.realpath(upload_dir):
        raise ValueError(f"Unsafe upload path rejected: {original_name!r}")

    with open(file_path, "wb") as f:
        f.write(file_bytes)

    file_size = len(file_bytes)
    now = datetime.now().isoformat()

    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO documents
               (project_id, filename, original_name, file_type, file_size, file_path, description, uploaded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                project_id,
                filename,
                original_name,
                file_type,
                file_size,
                file_path,
                description,
                now,
            ),
        )
        conn.commit()
        doc_id = cursor.lastrowid
    except Exception:
        try:
            os.remove(file_path)
        except OSError:
            pass
        raise
    finally:
        conn.close()

    audit(
        "document.upload",
        target=original_name,
        details=f"{file_type or 'file'}, {file_size} bytes",
        project_id=project_id,
    )

    return Document(
        id=doc_id,
        project_id=project_id,
        filename=filename,
        original_name=original_name,
        file_type=file_type,
        file_size=file_size,
        file_path=file_path,
        description=description,
        uploaded_at=now,
    )


def list_documents(project_id: int) -> list[Document]:
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM documents WHERE project_id = ? ORDER BY uploaded_at DESC",
        (project_id,),
    )
    rows = cursor.fetchall()
    conn.close()

    return [
        Document(
            id=row["id"],
            project_id=row["project_id"],
            filename=row["filename"],
            original_name=row["original_name"],
            file_type=row["file_type"],
            file_size=row["file_size"],
            file_path=_resolve_upload_path(row["project_id"], row["file_path"]),
            description=row["description"],
            uploaded_at=row["uploaded_at"],
        )
        for row in rows
    ]


def _resolve_upload_path(project_id: int, stored: str) -> str:
    """Locate an upload whose recorded path predates a move of the project.

    save_document() records an absolute path, so relocating the checkout — into
    OneDrive, onto another machine — orphans every existing document row while
    the files themselves sit happily under uploads/<project_id>/. Fall back to
    the current uploads dir before treating a document as missing. The stored
    path is returned unchanged when nothing is found, so callers still report
    the location they expected.
    """
    if not stored or os.path.exists(stored):
        return stored
    candidate = os.path.join(UPLOADS_DIR, str(project_id), os.path.basename(stored))
    return candidate if os.path.exists(candidate) else stored


def get_document(doc_id: int) -> Document | None:
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM documents WHERE id = ?", (doc_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return None

    return Document(
        id=row["id"],
        project_id=row["project_id"],
        filename=row["filename"],
        original_name=row["original_name"],
        file_type=row["file_type"],
        file_size=row["file_size"],
        file_path=_resolve_upload_path(row["project_id"], row["file_path"]),
        description=row["description"],
        uploaded_at=row["uploaded_at"],
    )


@dataclass
class SemanticModel:
    id: int | None = None
    project_id: int = 0
    wireframe_id: int | None = None
    model_name: str = ""
    server_fqdn: str = ""
    db_name: str = ""
    tmdl_files: dict[str, str] = None
    pbip_path: str = ""
    status: str = "generated"
    created_at: str = ""

    def __post_init__(self):
        if self.tmdl_files is None:
            self.tmdl_files = {}
        if not self.created_at:
            self.created_at = datetime.now().isoformat()


def save_semantic_model(
    project_id: int,
    model_name: str,
    tmdl_files: dict[str, str],
    wireframe_id: int | None = None,
    server_fqdn: str = "",
    db_name: str = "",
    pbip_path: str = "",
    langfuse_tracer=None,
) -> "SemanticModel":
    now = datetime.now().isoformat()
    with _db_cursor() as cursor:
        cursor.execute(
            """INSERT INTO semantic_models
               (project_id, wireframe_id, model_name, server_fqdn, db_name, tmdl_files, pbip_path, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                project_id,
                wireframe_id,
                model_name,
                server_fqdn,
                db_name,
                json.dumps(tmdl_files),
                pbip_path,
                now,
            ),
        )
        sm_id = cursor.lastrowid

    try:
        from evals.measure_correctness import run_measure_evals
        from evals.relationship_correctness import run_relationship_evals

        _, _m_score = run_measure_evals(
            tmdl_files, langfuse_tracer=langfuse_tracer, model_name=model_name
        )
        _, _r_score = run_relationship_evals(
            tmdl_files, langfuse_tracer=langfuse_tracer
        )
        _eval_log = os.path.join(os.path.dirname(DB_PATH), "logs", "eval_scores.jsonl")
        os.makedirs(os.path.dirname(_eval_log), exist_ok=True)
        with open(_eval_log, "a", encoding="utf-8") as _f:
            _f.write(
                json.dumps(
                    {
                        "timestamp": datetime.now().isoformat(timespec="seconds"),
                        "model_name": model_name,
                        "measure_score": _m_score,
                        "relationship_score": _r_score,
                    }
                )
                + "\n"
            )
    except Exception:
        pass

    _detail = f"{len(tmdl_files)} TMDL file(s)"
    try:
        _detail += f", measure={_m_score}, relationship={_r_score}"
    except NameError:
        pass
    audit("model.save", target=model_name, details=_detail, project_id=project_id)

    return SemanticModel(
        id=sm_id,
        project_id=project_id,
        wireframe_id=wireframe_id,
        model_name=model_name,
        server_fqdn=server_fqdn,
        db_name=db_name,
        tmdl_files=tmdl_files,
        pbip_path=pbip_path,
        created_at=now,
    )


def get_latest_semantic_model(project_id: int) -> Optional["SemanticModel"]:
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM semantic_models WHERE project_id = ? ORDER BY created_at DESC LIMIT 1",
        (project_id,),
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return SemanticModel(
        id=row["id"],
        project_id=row["project_id"],
        wireframe_id=row["wireframe_id"],
        model_name=row["model_name"],
        server_fqdn=row["server_fqdn"],
        db_name=row["db_name"],
        tmdl_files=json.loads(row["tmdl_files"] or "{}"),
        pbip_path=row["pbip_path"] or "",
        status=row["status"],
        created_at=row["created_at"],
    )


def list_semantic_models(project_id: int) -> list["SemanticModel"]:
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM semantic_models WHERE project_id = ? ORDER BY created_at DESC",
        (project_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    return [
        SemanticModel(
            id=row["id"],
            project_id=row["project_id"],
            wireframe_id=row["wireframe_id"],
            model_name=row["model_name"],
            server_fqdn=row["server_fqdn"],
            db_name=row["db_name"],
            tmdl_files=json.loads(row["tmdl_files"] or "{}"),
            pbip_path=row["pbip_path"] or "",
            status=row["status"],
            created_at=row["created_at"],
        )
        for row in rows
    ]


def update_semantic_model_pbip_path(sm_id: int, pbip_path: str):
    with _db_cursor() as cursor:
        cursor.execute(
            "UPDATE semantic_models SET pbip_path = ? WHERE id = ?", (pbip_path, sm_id)
        )


def delete_document(doc_id: int) -> bool:
    doc = get_document(doc_id)
    if not doc:
        return False

    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        conn.commit()
    finally:
        conn.close()

    if os.path.exists(doc.file_path):
        os.remove(doc.file_path)

    audit(
        "document.delete",
        target=doc.original_name,
        severity="warning",
        project_id=doc.project_id,
    )
    return True


_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff"}


def read_document_text(doc: Document) -> str:
    if not os.path.exists(doc.file_path):
        return f"[{doc.original_name}: file missing on disk]"

    ext = os.path.splitext(doc.file_path)[1].lower()
    file_type = (doc.file_type or "").lower()

    if file_type == "application/pdf" or ext == ".pdf":
        try:
            import io

            from pypdf import PdfReader

            with open(doc.file_path, "rb") as f:
                reader = PdfReader(io.BytesIO(f.read()))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            if len(text.strip()) < 50:
                return (
                    f"[{doc.original_name}: PDF returned <50 chars — likely a scanned "
                    f"image-only PDF. Add OCR or upload a text-bearing version.]"
                )
            return text
        except ImportError:
            return (
                f"[{doc.original_name}: pypdf not installed — run `pip install pypdf`]"
            )
        except Exception as e:
            return f"[{doc.original_name}: PDF parse failed — {e}]"

    if file_type.startswith("image/") or ext in _IMAGE_EXTS:
        return (
            f"[Image: {doc.original_name} — pass to vision model for OCR/description; "
            f"raw binary content not loaded as text]"
        )

    if ext in (".xlsx", ".xls", ".xlsm"):
        try:
            import openpyxl

            wb = openpyxl.load_workbook(doc.file_path, data_only=True)
            parts = []
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                rows = list(ws.iter_rows(values_only=True))
                if not rows:
                    continue
                parts.append(f"## Sheet: {sheet_name}")
                for row in rows:
                    parts.append(" | ".join("" if v is None else str(v) for v in row))
            return (
                "\n".join(parts)
                if parts
                else f"[{doc.original_name}: Excel file is empty]"
            )
        except ImportError:
            return f"[{doc.original_name}: openpyxl not installed — run `pip install openpyxl`]"
        except Exception as e:
            return f"[{doc.original_name}: Excel parse failed — {e}]"

    if ext in (".pptx", ".pptm"):
        try:
            from pptx import Presentation

            prs = Presentation(doc.file_path)
            parts = []
            for i, slide in enumerate(prs.slides, 1):
                slide_parts: list[str] = []
                for shape in slide.shapes:
                    if shape.has_text_frame and shape.text_frame.text.strip():
                        slide_parts.append(shape.text_frame.text.strip())
                    if getattr(shape, "has_table", False):
                        for row in shape.table.rows:
                            slide_parts.append(
                                " | ".join(c.text.strip() for c in row.cells)
                            )
                if slide.has_notes_slide:
                    notes = (slide.notes_slide.notes_text_frame.text or "").strip()
                    if notes:
                        slide_parts.append(f"_Speaker notes:_ {notes}")
                if slide_parts:
                    parts.append(f"## Slide {i}")
                    parts.extend(slide_parts)
            return (
                "\n".join(parts)
                if parts
                else f"[{doc.original_name}: PowerPoint file has no readable text]"
            )
        except ImportError:
            return f"[{doc.original_name}: python-pptx not installed — run `pip install python-pptx`]"
        except Exception as e:
            return f"[{doc.original_name}: PowerPoint parse failed — {e}]"

    if ext in (".docx", ".docm"):
        try:
            import docx

            document = docx.Document(doc.file_path)
            parts = [p.text for p in document.paragraphs if p.text.strip()]
            for i, table in enumerate(document.tables, 1):
                parts.append(f"## Table {i}")
                for row in table.rows:
                    parts.append(" | ".join(c.text.strip() for c in row.cells))
            return (
                "\n".join(parts)
                if parts
                else f"[{doc.original_name}: Word file has no readable text]"
            )
        except ImportError:
            return f"[{doc.original_name}: python-docx not installed — run `pip install python-docx`]"
        except Exception as e:
            return f"[{doc.original_name}: Word parse failed — {e}]"

    if ext == ".csv":
        try:
            with open(doc.file_path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
        except Exception as e:
            return f"[{doc.original_name}: CSV read failed — {e}]"

    try:
        with open(doc.file_path, "rb") as f:
            raw = f.read()
        if b"\x00" in raw[:4096]:
            return (
                f"[{doc.original_name}: binary file ({ext or 'no extension'}) — "
                f"no text extractor for this type]"
            )
        return raw.decode("utf-8", errors="ignore")
    except Exception as e:
        return f"[{doc.original_name}: read failed — {e}]"


MODEL_PRICING = {
    "claude-opus-4-7": {"input": 0.015, "output": 0.045},
    "claude-sonnet-4-6": {"input": 0.003, "output": 0.015},
    "claude-haiku-4-5": {"input": 0.00080, "output": 0.004},
}


@dataclass
class ApiUsage:
    id: int | None = None
    project_id: int = 0
    agent: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    status: str = "success"
    created_at: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()


def calculate_api_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pricing = MODEL_PRICING.get(model, {"input": 0.0, "output": 0.0})
    input_cost = (input_tokens / 1000) * pricing["input"]
    output_cost = (output_tokens / 1000) * pricing["output"]
    return round(input_cost + output_cost, 6)


def save_project_rule(project_id: int, category: str, rule_text: str) -> int:
    with _db_cursor() as cursor:
        cursor.execute(
            "INSERT INTO project_rules (project_id, category, rule_text, created_at) VALUES (?, ?, ?, ?)",
            (project_id, category, rule_text.strip(), datetime.now().isoformat()),
        )
        rid = cursor.lastrowid
    return rid


def list_project_rules(project_id: int, active_only: bool = True) -> list:
    conn = _get_connection()
    cursor = conn.cursor()
    if active_only:
        cursor.execute(
            "SELECT id, category, rule_text, is_active, created_at FROM project_rules "
            "WHERE project_id = ? AND is_active = 1 ORDER BY id DESC",
            (project_id,),
        )
    else:
        cursor.execute(
            "SELECT id, category, rule_text, is_active, created_at FROM project_rules "
            "WHERE project_id = ? ORDER BY id DESC",
            (project_id,),
        )
    rows = [
        {
            "id": r["id"],
            "category": r["category"],
            "rule_text": r["rule_text"],
            "is_active": bool(r["is_active"]),
            "created_at": r["created_at"],
        }
        for r in cursor.fetchall()
    ]
    conn.close()
    return rows


def deactivate_project_rule(rule_id: int, *, project_id: int) -> bool:
    with _db_cursor() as cursor:
        cursor.execute(
            "UPDATE project_rules SET is_active = 0 WHERE id = ? AND project_id = ?",
            (rule_id, project_id),
        )
        return cursor.rowcount > 0


def save_api_usage(
    project_id: int,
    agent: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int = 0,
    status: str = "success",
) -> ApiUsage:
    cost_usd = calculate_api_cost(model, input_tokens, output_tokens)
    now = datetime.now().isoformat()

    with _db_cursor() as cursor:
        cursor.execute(
            """INSERT INTO api_usage
               (project_id, agent, model, input_tokens, output_tokens, cost_usd, latency_ms, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                project_id,
                agent,
                model,
                input_tokens,
                output_tokens,
                cost_usd,
                latency_ms,
                status,
                now,
            ),
        )
        usage_id = cursor.lastrowid

    return ApiUsage(
        id=usage_id,
        project_id=project_id,
        agent=agent,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        status=status,
        created_at=now,
    )


def create_custom_skill(
    slug: str,
    name: str,
    description: str,
    body: str,
    created_by: str = "",
) -> int:
    now = datetime.now().isoformat()
    with _db_cursor() as cursor:
        cursor.execute(
            "INSERT INTO custom_skills (slug, name, description, body, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                slug.strip().lower(),
                name.strip(),
                description.strip(),
                body,
                created_by,
                now,
                now,
            ),
        )
        rid = cursor.lastrowid
    return rid


def update_custom_skill(
    skill_id: int,
    name: str | None = None,
    description: str | None = None,
    body: str | None = None,
) -> None:
    parts, params = [], []
    if name is not None:
        parts.append("name = ?")
        params.append(name.strip())
    if description is not None:
        parts.append("description = ?")
        params.append(description.strip())
    if body is not None:
        parts.append("body = ?")
        params.append(body)
    if not parts:
        return
    parts.append("updated_at = ?")
    params.append(datetime.now().isoformat())
    params.append(skill_id)
    with _db_cursor() as cursor:
        cursor.execute(
            f"UPDATE custom_skills SET {', '.join(parts)} WHERE id = ?", params
        )


def delete_custom_skill(skill_id: int) -> None:
    with _db_cursor() as cursor:
        cursor.execute("DELETE FROM custom_skills WHERE id = ?", (skill_id,))


def list_custom_skills(active_only: bool = True) -> list[dict]:
    conn = _get_connection()
    if active_only:
        rows = conn.execute(
            "SELECT id, slug, name, description, body, created_by, is_active, created_at, updated_at "
            "FROM custom_skills WHERE is_active = 1 ORDER BY name"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, slug, name, description, body, created_by, is_active, created_at, updated_at "
            "FROM custom_skills ORDER BY name"
        ).fetchall()
    conn.close()
    return [
        {
            "id": r["id"],
            "slug": r["slug"],
            "name": r["name"],
            "description": r["description"],
            "body": r["body"],
            "created_by": r["created_by"],
            "is_active": bool(r["is_active"]),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


def set_project_skill_enabled(
    project_id: int, skill_slug: str, is_enabled: bool
) -> None:
    with _db_cursor() as cursor:
        cursor.execute(
            "INSERT INTO project_skills (project_id, skill_slug, is_enabled, created_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(project_id, skill_slug) DO UPDATE SET is_enabled = excluded.is_enabled",
            (project_id, skill_slug, int(is_enabled), datetime.now().isoformat()),
        )


def get_project_skills(project_id: int) -> dict[str, bool]:
    conn = _get_connection()
    rows = conn.execute(
        "SELECT skill_slug, is_enabled FROM project_skills WHERE project_id = ?",
        (project_id,),
    ).fetchall()
    conn.close()
    return {r["skill_slug"]: bool(r["is_enabled"]) for r in rows}


def clear_project_skill_overrides(project_id: int) -> None:
    with _db_cursor() as cursor:
        cursor.execute("DELETE FROM project_skills WHERE project_id = ?", (project_id,))


if __name__ == "__main__":
    init_db()
    print(" Database initialized at:", DB_PATH)

    try:
        client = create_client("Acme Corp", "Financial services company")
        print(f" Created client: {client.name}")

        project = create_project(
            client.id, "Dashboard Redesign", "Modernize financial dashboard"
        )
        print(f" Created project: {project.name}")

        wf = save_wireframe(project.id, "Design Concept 1", "Blue theme with cards")
        print(f" Saved wireframe: {wf.title}")

        print(f"\nAll clients: {[c.name for c in list_clients()]}")
        print(
            f"Projects for {client.name}: {[p.name for p in list_projects(client.id)]}"
        )
    except Exception as e:
        print(f"Note: {e}")
