from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from config import (
    DAX_SPILL_DIR,
    DAX_SPILL_PAGE_MAX_ROWS,
    DAX_SPILL_PREVIEW_ROWS,
    DAX_SPILL_ROW_THRESHOLD,
)


def _extract_rows(parsed: dict | None) -> list[dict] | None:
    if not isinstance(parsed, dict):
        return None
    data = parsed.get("data")
    if isinstance(data, dict):
        rows = data.get("rows")
        if isinstance(rows, list) and rows and all(isinstance(r, dict) for r in rows):
            return rows
    return None


def _clean_col(name: str) -> str:
    return name.replace("[", " ").replace("]", "").strip()


def spill_if_large(parsed: dict | None, raw_text: str) -> str:
    try:
        rows = _extract_rows(parsed)
        if not rows or len(rows) <= DAX_SPILL_ROW_THRESHOLD:
            return raw_text

        columns = list(rows[0].keys())
        headers = [_clean_col(c) for c in columns]

        digest = hashlib.sha256(
            json.dumps(rows[:5], sort_keys=True, default=str).encode("utf-8")
            + str(len(rows)).encode("utf-8")
        ).hexdigest()[:12]
        DAX_SPILL_DIR.mkdir(parents=True, exist_ok=True)
        csv_path = DAX_SPILL_DIR / f"dax_{digest}.csv"

        if not csv_path.exists():
            with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(headers)
                for r in rows:
                    writer.writerow([r.get(c, "") for c in columns])

        preview = rows[:DAX_SPILL_PREVIEW_ROWS]
        summary = {
            "spilled": True,
            "row_count": len(rows),
            "columns": headers,
            "preview_rows": [{_clean_col(k): v for k, v in r.items()} for r in preview],
            "csv_path": str(csv_path),
            "note": (
                f"Result has {len(rows)} rows — full set saved to the CSV above; "
                f"only the first {len(preview)} shown. Read more with "
                "pbi_dax_page(csv_path, offset, limit). Prefer aggregating in "
                "DAX (SUMMARIZECOLUMNS/TOPN) over paging raw rows."
            ),
        }
        return json.dumps(summary, indent=2, default=str)
    except Exception:
        return raw_text


def page_csv(csv_path: str, offset: int = 0, limit: int = 50) -> str:
    try:
        path = Path(csv_path).resolve()
        spill_root = DAX_SPILL_DIR.resolve()
        if spill_root not in path.parents and path != spill_root:
            return f"Error: path must be under {spill_root} (got {csv_path})."
        if not path.is_file():
            return f"Error: spill file not found: {csv_path}"

        offset = max(0, int(offset))
        limit = max(1, min(int(limit), DAX_SPILL_PAGE_MAX_ROWS))

        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            header = next(reader, [])
            total = 0
            window: list[list[str]] = []
            for i, row in enumerate(reader):
                total += 1
                if offset <= i < offset + limit:
                    window.append(row)

        return json.dumps(
            {
                "columns": header,
                "total_rows": total,
                "offset": offset,
                "returned": len(window),
                "rows": window,
                "has_more": offset + len(window) < total,
            },
            indent=2,
            default=str,
        )
    except Exception as e:
        return f"Error paging spill file: {type(e).__name__}: {e}"
