import os
import sqlite3
import sys

sys.path.insert(0, os.getcwd())

from dotenv import load_dotenv  # noqa: E402

load_dotenv(".env", override=True)

from db import fabric_backend as fb  # noqa: E402

src = sqlite3.connect("project_data.db")
src.row_factory = sqlite3.Row
conn = fb.get_connection()
cur = conn.cursor()

tables = [
    r[0]
    for r in src.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
]

RESET = "--reset" in sys.argv

if RESET:
    # Plain INSERTs are not re-runnable, so a resync clears the target first.
    # Safe only while Fabric is not yet the system of record -- once it is,
    # this destroys data. Deliberately opt-in via --reset.
    for t in reversed(tables):
        cur.execute(f"DELETE FROM dbo.[{t}]")
    conn.commit()
    print("--reset: cleared all target tables")
    print()

total = 0
for t in tables:
    rows = src.execute(f'SELECT * FROM "{t}"').fetchall()
    if not rows:
        print(f"  {t:<26} 0 rows (skipped)")
        continue
    cols = rows[0].keys()
    # Does this table have an IDENTITY column? If so, preserve the ids.
    ident = cur.execute(
        "SELECT COUNT(*) AS n FROM sys.identity_columns "
        "WHERE object_id = OBJECT_ID(?)",
        (f"dbo.{t}",),
    ).fetchone()["n"]
    collist = ", ".join(f"[{c}]" for c in cols)
    marks = ", ".join("?" for _ in cols)
    if ident:
        cur.execute(f"SET IDENTITY_INSERT dbo.[{t}] ON")
    n = 0
    for r in rows:
        try:
            cur.execute(
                f"INSERT INTO dbo.[{t}] ({collist}) VALUES ({marks})",
                tuple(r[c] for c in cols),
            )
            n += 1
        except Exception as e:
            print(f"  {t}: row failed -- {str(e)[:120]}")
    if ident:
        cur.execute(f"SET IDENTITY_INSERT dbo.[{t}] OFF")
    conn.commit()
    print(f"  {t:<26} {n}/{len(rows)} rows")
    total += n

print(f"\nmigrated {total} rows")
conn.close()
