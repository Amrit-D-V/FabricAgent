import glob
import json
import os

import contextvars as _contextvars
from report.report_scan import ReportScan

_CURRENT_SCAN: "_contextvars.ContextVar" = _contextvars.ContextVar(
    "_hc_report_scan", default=None
)


import threading

_SCAN_LOCK = threading.RLock()
_REPORT_SCAN_CACHE: dict[str, ReportScan] = {}

_REPORT_SCAN_READERS = frozenset(
    {
        "pbi_report_info",
        "pbi_report_validate",
        "pbi_report_health_check",
        "pbi_visual_list",
        "pbi_dashboard_audit",
        "pbi_report_reload",
    }
)


def _report_scan_key(report_path: str) -> str:
    return os.path.normcase(os.path.abspath(report_path))


def _get_report_scan(report_path: str) -> ReportScan:
    key = _report_scan_key(report_path)
    with _SCAN_LOCK:
        scan = _REPORT_SCAN_CACHE.get(key)
        if scan is None:
            scan = ReportScan(report_path)
            _REPORT_SCAN_CACHE[key] = scan
        return scan


def _invalidate_report_scan(report_path: str) -> None:
    with _SCAN_LOCK:
        _REPORT_SCAN_CACHE.pop(_report_scan_key(report_path), None)



def _hc_glob(pattern: str, recursive: bool = False) -> list:
    scan = _CURRENT_SCAN.get()
    if scan is not None:
        return scan.glob(pattern, recursive=recursive)
    return glob.glob(pattern, recursive=recursive)


def _hc_read_json(path: str):
    scan = _CURRENT_SCAN.get()
    if scan is not None:
        return scan.read_json(path)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _hc_read_text(path: str) -> str:
    scan = _CURRENT_SCAN.get()
    if scan is not None:
        return scan.read_text(path)
    with open(path, encoding="utf-8") as f:
        return f.read()


def _hc_invalidate(path: str) -> None:
    scan = _CURRENT_SCAN.get()
    if scan is not None:
        scan.invalidate(path)
