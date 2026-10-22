from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

logger = logging.getLogger("fabricbi.jobs")

FABRIC_JOBS_DDL = """
IF NOT EXISTS (
    SELECT 1 FROM sys.tables t
    JOIN sys.schemas s ON s.schema_id = t.schema_id
    WHERE s.name = 'dbo' AND t.name = 'background_jobs'
)
CREATE TABLE dbo.background_jobs (
    id           NVARCHAR(64)   NOT NULL PRIMARY KEY,
    project_id   INT            NULL,
    job_type     NVARCHAR(100)  NOT NULL,
    status       NVARCHAR(50)   NOT NULL DEFAULT 'queued',
    progress_msg NVARCHAR(500)  NULL,
    result_json  NVARCHAR(MAX)  NULL,
    error_msg    NVARCHAR(MAX)  NULL,
    created_at   NVARCHAR(64)   NOT NULL,
    updated_at   NVARCHAR(64)   NOT NULL
)
"""

SQLITE_JOBS_DDL = """
CREATE TABLE IF NOT EXISTS background_jobs (
    id           TEXT           NOT NULL PRIMARY KEY,
    project_id   INTEGER        NULL,
    job_type     TEXT           NOT NULL,
    status       TEXT           NOT NULL DEFAULT 'queued',
    progress_msg TEXT           NULL,
    result_json  TEXT           NULL,
    error_msg    TEXT           NULL,
    created_at   TEXT           NOT NULL,
    updated_at   TEXT           NOT NULL
)
"""

_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="fabricbi_job_worker")
_INIT_LOCK = threading.Lock()
_INITIALIZED = False


def _get_db():
    from db.database import DB_BACKEND, _get_connection

    conn = _get_connection()
    table = "dbo.background_jobs" if DB_BACKEND == "fabric" else "background_jobs"
    return conn, table, DB_BACKEND


def init_jobs_table() -> None:
    global _INITIALIZED
    if _INITIALIZED:
        return
    with _INIT_LOCK:
        if _INITIALIZED:
            return
        try:
            conn, _table, backend = _get_db()
            ddl = FABRIC_JOBS_DDL if backend == "fabric" else SQLITE_JOBS_DDL
            conn.cursor().execute(ddl)
            conn.commit()
            if backend == "fabric":
                conn.close()
            _INITIALIZED = True
        except Exception as exc:
            logger.warning("Could not initialize background_jobs table: %s", exc)


def create_job(job_type: str, project_id: int | None = None) -> str:
    init_jobs_table()
    job_id = str(uuid.uuid4())
    now = datetime.now().isoformat()
    conn, table, backend = _get_db()
    try:
        conn.cursor().execute(
            f"""
            INSERT INTO {table}
            (id, project_id, job_type, status, progress_msg, result_json, error_msg, created_at, updated_at)
            VALUES (?, ?, ?, 'queued', 'Queued', NULL, NULL, ?, ?)
            """,
            (job_id, project_id, job_type, now, now),
        )
        conn.commit()
    finally:
        if backend == "fabric":
            conn.close()
    return job_id


def update_job_progress(job_id: str, message: str, status: str = "running") -> None:
    init_jobs_table()
    now = datetime.now().isoformat()
    conn, table, backend = _get_db()
    try:
        conn.cursor().execute(
            f"UPDATE {table} SET status = ?, progress_msg = ?, updated_at = ? WHERE id = ?",
            (status, message[:500], now, job_id),
        )
        conn.commit()
    finally:
        if backend == "fabric":
            conn.close()


def complete_job(job_id: str, result: Any) -> None:
    init_jobs_table()
    now = datetime.now().isoformat()
    raw_result = json.dumps(result, default=str)
    conn, table, backend = _get_db()
    try:
        conn.cursor().execute(
            f"UPDATE {table} SET status = 'completed', progress_msg = 'Done', result_json = ?, updated_at = ? WHERE id = ?",
            (raw_result, now, job_id),
        )
        conn.commit()
    finally:
        if backend == "fabric":
            conn.close()


def fail_job(job_id: str, error_msg: str) -> None:
    init_jobs_table()
    now = datetime.now().isoformat()
    conn, table, backend = _get_db()
    try:
        conn.cursor().execute(
            f"UPDATE {table} SET status = 'failed', progress_msg = 'Failed', error_msg = ?, updated_at = ? WHERE id = ?",
            (error_msg, now, job_id),
        )
        conn.commit()
    finally:
        if backend == "fabric":
            conn.close()


def get_job(job_id: str) -> dict | None:
    init_jobs_table()
    conn, table, backend = _get_db()
    try:
        row = conn.execute(
            f"SELECT id, project_id, job_type, status, progress_msg, result_json, error_msg, created_at, updated_at "
            f"FROM {table} WHERE id = ?",
            (job_id,),
        ).fetchone()
    finally:
        if backend == "fabric":
            conn.close()

    if not row:
        return None

    data = dict(row)
    if data.get("result_json"):
        try:
            data["result"] = json.loads(data["result_json"])
        except Exception:
            data["result"] = data["result_json"]
    else:
        data["result"] = None
    return data


def submit_background_job(
    job_type: str,
    target_fn: Callable[[Callable[[str], None]], Any],
    project_id: int | None = None,
) -> str:
    init_jobs_table()
    job_id = create_job(job_type, project_id=project_id)

    def _runner():
        update_job_progress(job_id, "Started", status="running")
        try:
            def on_progress(msg: str):
                update_job_progress(job_id, msg, status="running")

            res = target_fn(on_progress)
            complete_job(job_id, res)
        except Exception as e:
            logger.exception("Background job %s (%s) failed", job_id, job_type)
            fail_job(job_id, str(e))

    _POOL.submit(_runner)
    return job_id
