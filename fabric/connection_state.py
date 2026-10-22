from __future__ import annotations

import json
from pathlib import Path

_FILE = Path.home() / ".pbi_agent" / "last_connection.json"


def load_last_connection() -> tuple[str, list[str]]:
    try:
        data = json.loads(_FILE.read_text(encoding="utf-8"))
        ws = data.get("workspace", "") or ""
        models = [m for m in data.get("models", []) if isinstance(m, str) and m]
        return ws, models
    except Exception:
        return "", []


def save_last_connection(workspace: str, models: list[str]) -> None:
    try:
        _FILE.parent.mkdir(exist_ok=True)
        _FILE.write_text(
            json.dumps({"workspace": workspace, "models": list(models)}),
            encoding="utf-8",
        )
    except Exception:
        pass
