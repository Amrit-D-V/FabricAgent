
from __future__ import annotations

import glob
import json
import os

from config import PROJECTS_DIR


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _parses_as_json(path: str) -> bool:
    try:
        with open(path, encoding="utf-8-sig") as f:
            json.load(f)
        return True
    except (OSError, json.JSONDecodeError):
        return False


def _dataset_name(report_dir: str) -> str:
    ref = (
        _read_json(os.path.join(report_dir, "definition.pbir")).get("datasetReference")
        or {}
    )
    by_path = (ref.get("byPath") or {}).get("path") or ""
    if by_path:
        return os.path.basename(by_path.rstrip("/\\")).replace(".SemanticModel", "")
    return (ref.get("byConnection") or {}).get("datasetId") or ""


def scan_report(report_dir: str) -> dict:
    pages_dir = os.path.join(report_dir, "definition", "pages")
    pages: list[dict] = []
    if os.path.isdir(pages_dir):
        order = _read_json(os.path.join(pages_dir, "pages.json")).get("pageOrder") or []
        found = [
            d
            for d in sorted(os.listdir(pages_dir))
            if os.path.isfile(os.path.join(pages_dir, d, "page.json"))
        ]
        for name in list(order) + [f for f in found if f not in order]:
            pj = _read_json(os.path.join(pages_dir, name, "page.json"))
            pages.append(
                {
                    "name": name,
                    "display_name": pj.get("displayName", name),
                    "hidden": pj.get("visibility") == "HiddenInViewMode",
                    "visuals": sum(
                        1
                        for v in glob.glob(
                            os.path.join(pages_dir, name, "visuals", "*", "visual.json")
                        )
                        if _parses_as_json(v)
                    ),
                }
            )
        fmt = "PBIR"
    elif os.path.isfile(os.path.join(report_dir, "report.json")):
        sections = _read_json(os.path.join(report_dir, "report.json")).get("sections")
        pages = [{"name": s.get("name", "?"), "visuals": -1} for s in (sections or [])]
        fmt = "legacy"
    else:
        fmt = "unknown"

    return {
        "path": report_dir,
        "name": os.path.basename(report_dir).replace(".Report", ""),
        "project": os.path.relpath(report_dir, str(PROJECTS_DIR)).split(os.sep)[0],
        "format": fmt,
        "model": _dataset_name(report_dir),
        "page_count": len(pages),
        "visual_count": sum(p["visuals"] for p in pages if p["visuals"] > 0),
        "pages": pages,
        "publishable": bool(pages) and any(p["visuals"] != 0 for p in pages),
    }


def list_reports(root: str = "") -> list[dict]:
    base = root or str(PROJECTS_DIR)
    dirs = [
        d
        for d in glob.glob(os.path.join(base, "**", "*.Report"), recursive=True)
        if os.path.isdir(d) and ".backup" not in d
    ]
    reports = [scan_report(d) for d in dirs]
    reports.sort(key=lambda r: os.path.getmtime(r["path"]), reverse=True)
    return reports


def format_catalog(reports: list[dict]) -> str:
    if not reports:
        return f"No reports found under {PROJECTS_DIR}."
    out = [f"{len(reports)} report(s) under {PROJECTS_DIR}:"]
    for r in reports:
        flag = "" if r["publishable"] else "  [NOT PUBLISHABLE — no pages/visuals]"
        model = f" -> model {r['model']}" if r["model"] else ""
        out.append(
            f"\n{r['project']}/{r['name']}  ({r['format']}, {r['page_count']} page(s), "
            f"{r['visual_count']} visual(s)){model}{flag}"
        )
        out.append(f"  path: {r['path']}")
        for p in r["pages"]:
            vis = "?" if p["visuals"] < 0 else p["visuals"]
            hidden = " (hidden)" if p.get("hidden") else ""
            out.append(
                f"    - {p['name']}: {p.get('display_name', p['name'])}"
                f"{hidden} — {vis} visual(s)"
            )
    return "\n".join(out)
