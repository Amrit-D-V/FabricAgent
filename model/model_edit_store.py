from __future__ import annotations

import json
import os

from config import PROJECT_ROOT

_STORE_DIR = str(PROJECT_ROOT)


def _pending_path() -> str:
    return os.path.join(_STORE_DIR, ".pending_model_edits.json")


def _ledger_path(session_id: str) -> str:
    safe = "".join(c for c in (session_id or "default") if c.isalnum() or c in "-_")
    return os.path.join(_STORE_DIR, f".created_model_objects_{safe or 'default'}.json")


def _read(path: str) -> list:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _write(path: str, rows: list) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)


def add_pending(edit: dict) -> dict:
    rows = _read(_pending_path())
    eid = f"e{len(rows) + 1}_{abs(hash(json.dumps(edit, sort_keys=True))) % 100000}"
    record = dict(edit)
    record["id"] = eid
    record["status"] = "pending"
    rows.append(record)
    _write(_pending_path(), rows)
    return record


def list_pending(status: str | None = "pending") -> list:
    rows = _read(_pending_path())
    if status is None:
        return rows
    return [r for r in rows if r.get("status") == status]


def set_status(edit_id: str, status: str) -> bool:
    rows = _read(_pending_path())
    hit = False
    for r in rows:
        if r.get("id") == edit_id:
            r["status"] = status
            hit = True
    if hit:
        _write(_pending_path(), rows)
    return hit


def add_to_ledger(session_id: str, obj: dict) -> None:
    rows = _read(_ledger_path(session_id))
    rows.append(obj)
    _write(_ledger_path(session_id), rows)


def read_ledger(session_id: str) -> list:
    return _read(_ledger_path(session_id))


def clear_ledger(session_id: str) -> None:
    try:
        os.remove(_ledger_path(session_id))
    except OSError:
        pass
