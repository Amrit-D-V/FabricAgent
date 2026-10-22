
from __future__ import annotations

import json
import os

from config import SNAPSHOT_REPORT_CONTENT

_BUILD_TRACE_PATH = os.path.join(os.path.dirname(__file__), "logs", "build_trace.log")


def _build_trace(event: str, **payload) -> None:
    try:
        import time as _time

        os.makedirs(os.path.dirname(_BUILD_TRACE_PATH), exist_ok=True)
        entry = {"ts": _time.time(), "event": event}
        for k, v in payload.items():
            if isinstance(v, str) and len(v) > 20000:
                entry[k] = v[:20000] + f"...[truncated {len(v) - 20000} chars]"
            else:
                entry[k] = v
        with open(_BUILD_TRACE_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _trace_file_after_write(path: str, source: str) -> None:
    try:
        if SNAPSHOT_REPORT_CONTENT:
            with open(path, encoding="utf-8") as _f:
                content = _f.read()
            _build_trace(
                "file_written",
                source=source,
                path=path.replace("\\", "/"),
                bytes=len(content),
                content=content,
            )
        else:
            _build_trace(
                "file_written",
                source=source,
                path=path.replace("\\", "/"),
                bytes=_safe_size(path),
            )
    except Exception as e:
        _build_trace(
            "file_read_error", source=source, path=path.replace("\\", "/"), error=str(e)
        )


def _safe_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return -1


def _snapshot_report(report_path: str, source: str) -> None:
    try:
        want_content = SNAPSHOT_REPORT_CONTENT
        for dp, _dns, fns in os.walk(report_path):
            for f in fns:
                if not f.endswith(
                    (".json", ".pbir", ".pbip", ".pbism", ".tmdl", ".platform")
                ):
                    continue
                p = os.path.join(dp, f)
                if not want_content:
                    _build_trace(
                        "snapshot_file",
                        source=source,
                        path=p.replace("\\", "/"),
                        bytes=_safe_size(p),
                    )
                    continue
                try:
                    with open(p, encoding="utf-8") as fh:
                        content = fh.read()
                    _build_trace(
                        "snapshot_file",
                        source=source,
                        path=p.replace("\\", "/"),
                        bytes=len(content),
                        content=content,
                    )
                except Exception as e:
                    _build_trace(
                        "snapshot_read_error",
                        source=source,
                        path=p.replace("\\", "/"),
                        error=str(e),
                    )
        _build_trace(
            "snapshot_done", source=source, root=report_path.replace("\\", "/")
        )
    except Exception as e:
        _build_trace("snapshot_error", source=source, error=str(e))
