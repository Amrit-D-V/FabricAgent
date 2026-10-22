from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

from config import PBI_DESKTOP_CLI, PBI_DESKTOP_EXE
from util.logger_setup import get_logger

log = get_logger(__name__)


class BridgeNotAvailable(RuntimeError):
    pass


def _run(*args: str, timeout: int = 30) -> dict | list:
    cli = shutil.which(PBI_DESKTOP_CLI)
    if not cli:
        raise BridgeNotAvailable(
            f"`{PBI_DESKTOP_CLI}` not found on PATH. "
            "Run: npm install -g @microsoft/powerbi-desktop-bridge-cli"
        )
    cmd = [cli, *args]
    log.debug("bridge: %s", " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    raw = (r.stdout or r.stderr or "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw, "_returncode": r.returncode}


def get_status() -> list[dict]:
    data = _run("status", timeout=10)
    if isinstance(data, list):  # older CLI: bare array
        return data
    if data.get("status") == "not_connected":
        raise BridgeNotAvailable(
            "Desktop Bridge not connected. Enable it under: "
            "File → Options → Preview Features → "
            "'Enable external tool access to Power BI Desktop through secure local APIs', "
            "then restart Desktop."
        )
    return data.get("instances") or []


def find_pid(pbip_path: str | Path) -> int | None:
    target = str(Path(pbip_path).resolve()).lower()
    for inst in get_status():
        if (inst.get("currentFilePath") or "").lower() == target:
            return inst["pid"]
    return None


def open_file(pbip_path: str | Path) -> None:
    _run("open", str(pbip_path), timeout=30)


def ensure_pid(
    pbip_path: str | Path,
    *,
    poll_s: float = 1.5,
    retries: int = 12,
) -> int:
    pid = find_pid(pbip_path)
    if pid:
        return pid

    instances = get_status()
    if not instances:
        log.info("Desktop not running — launching %s", PBI_DESKTOP_EXE)
        subprocess.Popen([PBI_DESKTOP_EXE, str(pbip_path)])
    else:
        open_file(pbip_path)

    for _ in range(retries):
        time.sleep(poll_s)
        pid = find_pid(pbip_path)
        if pid:
            return pid

    raise RuntimeError(
        f"Desktop did not open {pbip_path} within {int(poll_s * retries)}s. "
        "Open the file manually in Desktop and retry."
    )


def reload(pid: int) -> None:
    result = _run("reload", "--pid", str(pid), timeout=30)
    log.info("bridge reload pid=%d -> %s", pid, result)


def screenshot_all(pid: int, output_dir: str | Path, scale: int = 2) -> list[Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    _run(
        "screenshot-all",
        "--pid",
        str(pid),
        "--output-dir",
        str(out),
        "--scale",
        str(scale),
        timeout=90,
    )
    return sorted(out.glob("*.png"))
