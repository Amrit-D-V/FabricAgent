"""CLI to pull reports out of Fabric onto disk as PBIP projects.

The work lives in `fabric.report_fetch`, which the `pbi_report_fetch` assistant
tool also calls -- this is just the command-line front end, so a bulk export and
what FabricBI does in chat can never drift apart.

    python scripts/pbix_to_pbip.py --list
    python scripts/pbix_to_pbip.py --report "DIAD Final Report"
    python scripts/pbix_to_pbip.py --report "DIAD Final Report" --pbix
    python scripts/pbix_to_pbip.py --all

WHY NOT JUST UNZIP THE PBIX
A .pbix is a ZIP, so `zipfile.extractall()` succeeds -- but what comes out is
not PBIP and not source code. `DataModel` is a compressed Analysis Services
stream, not TMDL, and `Report/Layout` is one UTF-16 JSON blob, not the
`definition/pages/<page>/visuals/<visual>/visual.json` tree the report tools
glob for. The real conversion is the item-definition API, which is what
`fabric.report_fetch` uses. `--pbix` additionally saves the binary via the
Export API when you want the original file itself.

Output goes to FABRICBI_TEMP_DIR (PROJECTS_DIR/fabricbi_temp) unless --out is given.
Read-only against the tenant.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fabric import report_fetch as rf


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report", help="Report name to fetch (exact, case-insensitive)")
    ap.add_argument("--all", action="store_true", help="Fetch every report")
    ap.add_argument("--list", action="store_true", help="List reports and exit")
    ap.add_argument("--pbix", action="store_true", help="Also download the .pbix")
    ap.add_argument("--out", help="Destination root (default: FABRICBI_TEMP_DIR)")
    args = ap.parse_args()

    try:
        conn = rf.connection()
        print(f"  credentials: {conn.source} | workspace {conn.workspace!r}")
        reports = rf.list_reports(conn, rf.token(conn, rf._PBI_SCOPE))
    except rf.FetchError as e:
        print(f"ERROR - {e}", file=sys.stderr)
        return 1

    if not reports:
        print("ERROR - no reports visible with these credentials.", file=sys.stderr)
        return 1

    if args.list or not (args.report or args.all):
        print(f"\n{len(reports)} report(s) in {conn.workspace!r}:")
        for rep in sorted(reports, key=lambda x: (x.get("name") or "").lower()):
            print(f"  - {rep.get('name')}  [id {rep.get('id')}]")
        if not (args.report or args.all):
            print('\nPass --report "<name>" or --all to fetch.')
        return 0

    names = [r.get("name") or "" for r in reports] if args.all else [args.report]
    dest = Path(args.out) if args.out else None

    done = 0
    for name in names:
        try:
            res = rf.fetch_report(name, include_pbix=args.pbix, dest_root=dest)
        except rf.FetchError as e:
            print(f"\n{name}\n    ERROR - {e}", file=sys.stderr)
            continue
        print(f"\n{rf.format_result(res)}")
        if res.ok:
            done += 1

    print(f"\n{done}/{len(names)} report(s) fetched.")
    return 0 if done else 1


if __name__ == "__main__":
    raise SystemExit(main())
