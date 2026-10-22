import os
import sqlite3
import sys

sys.path.insert(0, os.getcwd())

from dotenv import load_dotenv  # noqa: E402

load_dotenv(".env", override=True)

from db import fabric_backend as fb  # noqa: E402

src = sqlite3.connect("project_data.db")
conn = fb.get_connection()
cur = conn.cursor()
tables = [
    r[0]
    for r in src.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
]
bad = 0
print(f"{'table':<26}{'sqlite':>8}{'fabric':>8}  ")
print("-" * 46)
for t in tables:
    a = src.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
    b = cur.execute(f"SELECT COUNT(*) AS n FROM dbo.[{t}]").fetchone()["n"]
    flag = "OK" if a == b else "MISMATCH"
    if a != b:
        bad += 1
    print(f"{t:<26}{a:>8}{b:>8}  {flag}")
print()
print("ALL MATCH" if bad == 0 else f"{bad} MISMATCHED")
# spot-check real content survived
r = cur.execute("SELECT email, role FROM dbo.app_users ORDER BY email").fetchall()
print(f"\napp_users in Fabric: {len(r)} -> {r[0]['email']} ({r[0]['role']})")
# Row counts alone would not notice a truncated NVARCHAR, so compare the actual
# blobs. Read the SQLite side live rather than against a remembered number -- a
# hardcoded 28,574 reported a mismatch that did not exist, because the local
# file had been written to since the constant was taken.
mismatched = 0
for (pid,) in src.execute("SELECT id FROM projects ORDER BY id"):
    a = src.execute("SELECT metadata FROM projects WHERE id=?", (pid,)).fetchone()[0]
    b = cur.execute("SELECT metadata FROM dbo.projects WHERE id=?", (pid,)).fetchone()[
        "metadata"
    ]
    same = (a or "") == (b or "")
    mismatched += not same
    print(f"  project {pid:<4} metadata {len(a or ''):>7,} chars  identical={same}")
print("\nblobs identical" if not mismatched else f"\n{mismatched} BLOB(S) DIFFER")
conn.close()
