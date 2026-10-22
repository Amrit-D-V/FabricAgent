from __future__ import annotations

import glob as _glob
import json as _json
import os


class ReportScan:

    def __init__(self, report_path: str):
        self.root = report_path
        self._glob_cache: dict[tuple[str, bool], list[str]] = {}
        self._text_cache: dict[str, str] = {}

    @staticmethod
    def _key(path: str) -> str:
        return os.path.normcase(os.path.abspath(path))

    def glob(self, pattern: str, recursive: bool = False) -> list[str]:
        key = (pattern, recursive)
        hit = self._glob_cache.get(key)
        if hit is None:
            hit = _glob.glob(pattern, recursive=recursive)
            self._glob_cache[key] = hit
        return hit

    def _text(self, path: str) -> str:
        k = self._key(path)
        cached = self._text_cache.get(k)
        if cached is not None:
            return cached
        with open(path, encoding="utf-8") as f:
            data = f.read()
        self._text_cache[k] = data
        return data

    def read_json(self, path: str):
        return _json.loads(self._text(path))

    def read_text(self, path: str) -> str:
        return self._text(path)

    def invalidate(self, path: str) -> None:
        self._text_cache.pop(self._key(path), None)
        self._glob_cache.clear()
