"""Fabric SQL spike: connectivity, latency measurement, and app_users migration.

The point of this script is to replace an estimate with a number. Moving app
storage from SQLite (microsecond reads) to Fabric SQL (network round trips) is
only sane if a typical page render stays responsive, so measure first and
migrate the other 16 tables only if these numbers justify it.

Read-only by default: it connects, times round trips, and lists what is already
in the database. Nothing is created or written without --apply.

    python scripts/fabric_spike.py                 # measure only
    python scripts/fabric_spike.py --apply         # + create table, copy users

Requires ODBC Driver 18 for SQL Server and AZURE_CLIENT_ID / AZURE_TENANT_ID /
AZURE_CLIENT_SECRET in .env (service-principal auth).
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

DEFAULT_SERVER = os.getenv(
    "FABRIC_SPIKE_SERVER",
    "your-server.database.fabric.microsoft.com,1433"
)
DEFAULT_DATABASE = os.getenv("FABRIC_SPIKE_DB", "fabricbi_db")

SQLITE_SOURCE = os.getenv("SQLITE_SOURCE", str(Path(__file__).resolve().parent.parent / "project_data.db"))


def measure(conn, n: int = 10) -> None:
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        conn.execute("SELECT 1").fetchone()
        samples.append((time.perf_counter() - t0) * 1000)

    samples.sort()
    print(f"\n--- round-trip latency over {n} calls (ms) ---")
    print(f"  min    {samples[0]:7.1f}")
    print(f"  median {statistics.median(samples):7.1f}")
    print(f"  max    {samples[-1]:7.1f}")

    median = statistics.median(samples)
    print(
        "\n  A sidebar render issuing ~20 queries would cost ~%.1fs"
        % (median * 20 / 1000)
    )
    print("  The same render on local SQLite costs single-digit milliseconds.")


def show_existing(conn) -> None:
    rows = conn.execute(
        "SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_TYPE = 'BASE TABLE' ORDER BY 1, 2"
    ).fetchall()
    print(f"\n--- existing tables ({len(rows)}) ---")
    for r in rows:
        print("   {}.{}".format(r["TABLE_SCHEMA"], r["TABLE_NAME"]))


def read_sqlite_users(path: str) -> list[dict]:
    if not os.path.exists(path):
        raise SystemExit(f"SQLite source not found: {path}")

    src = sqlite3.connect("file:{}?mode=ro".format(path.replace("\\", "/")), uri=True)
    src.row_factory = sqlite3.Row
    try:
        rows = src.execute(
            "SELECT email, password_hash, role, created_at, is_active FROM app_users"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        src.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--apply",
        action="store_true",
        help="create dbo.app_users and copy users across",
    )
    ap.add_argument(
        "--server", default=os.getenv("FABRIC_SPIKE_SERVER", DEFAULT_SERVER)
    )
    ap.add_argument(
        "--database", default=os.getenv("FABRIC_SPIKE_DB", DEFAULT_DATABASE)
    )
    ap.add_argument("--sqlite", default=SQLITE_SOURCE)
    args = ap.parse_args()

    os.environ["FABRIC_APP_DB_SERVER"] = args.server
    os.environ["FABRIC_APP_DB_NAME"] = args.database

    print(f"server   {args.server}")
    print(f"database {args.database}")
    print("mode     %s" % ("APPLY (writes)" if args.apply else "read-only"))

    from db import fabric_backend as fb

    t0 = time.perf_counter()
    conn = fb.get_connection()
    print("\nconnected in %.1fs" % (time.perf_counter() - t0))

    try:
        measure(conn)
        show_existing(conn)
    finally:
        conn.close()

    if not args.apply:
        print(
            "\nRead-only run. Re-run with --apply to create dbo.app_users "
            "and copy users across."
        )
        return

    users = read_sqlite_users(args.sqlite)
    print("\n--- applying ---")
    print(f"read {len(users)} users from SQLite")

    fb.init_app_users()
    print("dbo.app_users ready")

    for u in users:
        fb.upsert_app_user(
            email=u["email"],
            password_hash=u["password_hash"],
            role=u["role"] or "editor",
            created_at=u["created_at"] or "",
            is_active=bool(u["is_active"]),
        )
    print(f"upserted {len(users)} users")

    after = fb.list_app_users()
    print(f"\n--- verification: {len(after)} rows in Fabric ---")
    for r in after:
        print(f"   {r['email']:<40} {r['role']:<8} active={bool(r['is_active'])}")

    if len(after) != len(users):
        raise SystemExit(
            f"MISMATCH: {len(users)} source users, {len(after)} landed"
        )
    print("\nRow counts match.")


if __name__ == "__main__":
    main()
