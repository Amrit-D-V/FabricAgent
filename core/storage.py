from __future__ import annotations

import io
import os
import zipfile
from datetime import datetime
from pathlib import Path

from config import DAX_SPILL_DIR, FABRICBI_TEMP_DIR, LOGS_DIR, PROJECTS_DIR

try:
    from db.database import UPLOADS_DIR as _UPLOADS_DIR
except ImportError:
    _UPLOADS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "uploads")

ROOTS: dict[str, tuple[Path, str]] = {
    "Projects": (
        Path(PROJECTS_DIR),
        "PBIP projects FabricBI builds -- model/, report/.",
    ),
    "Fabric scratch": (
        Path(FABRICBI_TEMP_DIR),
        "Reports pulled down from Fabric for analysis. Pruned after 7 days.",
    ),
    "Uploads": (
        Path(_UPLOADS_DIR),
        "Documents attached to projects, one folder per project id.",
    ),
    "DAX spills": (
        Path(DAX_SPILL_DIR),
        "Query results spilled to disk when a DAX result is too large to inline.",
    ),
    "Logs": (Path(LOGS_DIR), "Application log, wireframe renders, eval scores."),
}

MAX_PREVIEW_BYTES = 1_000_000
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
MAX_ZIP_FILES = 2_000

TEXT_SUFFIXES = {
    ".txt", ".md", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".csv", ".tsv", ".log", ".py", ".js", ".ts", ".tsx", ".html", ".css", ".sql",
    ".tmdl", ".bim", ".pbir", ".pbip", ".rdl", ".xml", ".sh", ".ps1", ".env",
}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}


def safe_resolve(root: Path, rel: str) -> Path | None:
    try:
        target = (root / rel).resolve()
        root_r = root.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if target != root_r and not target.is_relative_to(root_r):
        return None
    return target


def fmt_size(n: int) -> str:
    step = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if step < 1024 or unit == "TB":
            return f"{step:.0f} {unit}" if unit == "B" else f"{step:.1f} {unit}"
        step /= 1024
    return f"{n} B"


def dir_size(path: Path) -> tuple[int, int]:
    total = files = 0
    for dirpath, _dirnames, filenames in os.walk(path, onerror=lambda _e: None):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
                files += 1
            except OSError:
                continue
    return total, files


def zip_tree(path: Path, arc_root: str) -> tuple[bytes, int, bool]:
    buf = io.BytesIO()
    added = 0
    truncated = False
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, _dirnames, filenames in os.walk(path, onerror=lambda _e: None):
            for name in sorted(filenames):
                if added >= MAX_ZIP_FILES:
                    truncated = True
                    break
                full = Path(dirpath) / name
                try:
                    zf.write(full, Path(arc_root) / full.relative_to(path))
                    added += 1
                except (OSError, ValueError):
                    continue
            if truncated:
                break
    return buf.getvalue(), added, truncated


def overview() -> list[dict]:
    out = []
    for label, (path, description) in ROOTS.items():
        exists = path.exists()
        size, count = dir_size(path) if exists else (0, 0)
        out.append(
            {
                "label": label,
                "path": str(path),
                "description": description,
                "exists": exists,
                "bytes": size,
                "size": fmt_size(size) if exists else "--",
                "files": count,
            }
        )
    return out


def resolve(root_label: str, rel: str) -> tuple[Path, Path] | None:
    entry = ROOTS.get(root_label)
    if not entry:
        return None
    root = entry[0]
    target = safe_resolve(root, rel or "")
    return (root, target) if target else None


def _entry(path: Path, root: Path) -> dict:
    try:
        stat = path.stat()
        size, mtime = stat.st_size, stat.st_mtime
    except OSError:
        size, mtime = 0, 0
    return {
        "name": path.name,
        "path": str(path.relative_to(root)).replace(os.sep, "/"),
        "isDir": path.is_dir(),
        "bytes": size,
        "size": fmt_size(size),
        "modified": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M") if mtime else "",
    }


def list_dir(root_label: str, rel: str = "") -> dict:
    found = resolve(root_label, rel)
    if not found:
        return {"error": "That location is not available."}
    root, target = found
    if not target.is_dir():
        return {"error": "Not a folder."}

    try:
        children = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as exc:
        return {"error": f"Cannot read folder: {exc}"}

    return {
        "root": root_label,
        "path": rel,
        "entries": [_entry(c, root) for c in children],
    }


def preview(root_label: str, rel: str) -> dict:
    found = resolve(root_label, rel)
    if not found:
        return {"kind": "error", "message": "That file is not available."}
    _root, target = found
    if not target.is_file():
        return {"kind": "error", "message": "That file is no longer available."}

    size = target.stat().st_size
    suffix = target.suffix.lower()

    if size == 0:
        return {"kind": "empty", "size": fmt_size(size)}
    if suffix in IMAGE_SUFFIXES:
        return {"kind": "image", "size": fmt_size(size)}
    if suffix in TEXT_SUFFIXES or suffix == "":
        try:
            with open(target, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read(MAX_PREVIEW_BYTES)
        except OSError as exc:
            return {"kind": "error", "message": f"Cannot read file: {exc}"}
        return {
            "kind": "text",
            "text": text,
            "size": fmt_size(size),
            "truncated": size > MAX_PREVIEW_BYTES,
        }
    return {"kind": "binary", "size": fmt_size(size)}
