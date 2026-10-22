from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from config import PROJECT_ROOT

_SCHEMA_DIR = PROJECT_ROOT / "tools" / "pbir-schemas"
_URL_PREFIX = "https://developer.microsoft.com/json-schemas/"
_MAX_ERRORS_PER_FILE = 3


def _local_path(url: str) -> Path | None:
    if not isinstance(url, str) or not url.startswith(_URL_PREFIX):
        return None
    p = _SCHEMA_DIR / url[len(_URL_PREFIX) :].split("#")[0]
    return p if p.is_file() else None


@lru_cache(maxsize=1)
def _registry():
    from typing import Any

    import referencing
    import referencing.jsonschema
    from referencing.exceptions import NoSuchResource

    def retrieve(uri: str):
        p = _local_path(uri)
        if p is None:
            raise NoSuchResource(uri)
        contents = json.loads(p.read_text(encoding="utf-8-sig"))
        return referencing.Resource.from_contents(
            contents, default_specification=referencing.jsonschema.DRAFT7
        )

    registry_cls: Any = referencing.Registry
    return registry_cls(retrieve=retrieve)


def _version_tuple(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return ()


def _nearest_vendored(schema_url: str) -> str | None:
    rel = schema_url[len(_URL_PREFIX) :]
    parts = rel.split("/")
    if len(parts) < 3:
        return None
    declared = _version_tuple(parts[-2])
    type_dir = _SCHEMA_DIR / "/".join(parts[:-2])
    if not declared or not type_dir.is_dir():
        return None
    candidates = [
        v.name
        for v in type_dir.iterdir()
        if v.is_dir()
        and (v / "schema.json").is_file()
        and () < _version_tuple(v.name) <= declared
    ]
    if not candidates:
        return None
    best = max(candidates, key=_version_tuple)
    return _URL_PREFIX + "/".join(parts[:-2]) + f"/{best}/schema.json"


@lru_cache(maxsize=32)
def _validator_for(schema_url: str):
    import jsonschema

    exact = True
    p = _local_path(schema_url)
    if p is None:
        fallback = _nearest_vendored(schema_url or "")
        p = _local_path(fallback) if fallback else None
        exact = False
        if p is None:
            return None, True
    schema = json.loads(p.read_text(encoding="utf-8-sig"))
    cls = jsonschema.validators.validator_for(schema)
    return cls(schema, registry=_registry()), exact


def schemas_available() -> bool:
    return _SCHEMA_DIR.is_dir() and any(_SCHEMA_DIR.rglob("*.json"))


def check_pbir_schemas(
    report_path: str, scan=None, max_findings: int = 12
) -> list[dict]:
    findings: list[dict] = []
    if not schemas_available():
        return findings
    definition = os.path.join(report_path, "definition")
    root = definition if os.path.isdir(definition) else report_path
    for dirpath, _dirs, files in os.walk(root):
        if "StaticResources" in dirpath:
            continue
        for fname in files:
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                if scan is not None:
                    doc = scan.read_json(fpath)
                else:
                    with open(fpath, encoding="utf-8-sig") as f:
                        doc = json.load(f)
            except Exception:
                findings.append(
                    {
                        "check": "pbir_schema",
                        "severity": "error",
                        "file": os.path.relpath(fpath, report_path),
                        "message": f"{fname} is not valid JSON.",
                    }
                )
                continue
            if not isinstance(doc, dict):
                continue
            validator, exact = _validator_for(doc.get("$schema", ""))
            if validator is None:
                continue
            errors = sorted(validator.iter_errors(doc), key=lambda e: str(e.json_path))
            if not exact:
                errors = [
                    e
                    for e in errors
                    if e.validator != "additionalProperties"
                    and list(e.absolute_path) != ["$schema"]
                ]
            errors = errors[:_MAX_ERRORS_PER_FILE]
            for e in errors:
                findings.append(
                    {
                        "check": "pbir_schema",
                        "severity": "error",
                        "file": os.path.relpath(fpath, report_path),
                        "path": e.json_path,
                        "message": e.message[:300],
                    }
                )
            if len(findings) >= max_findings:
                return findings[:max_findings]
    return findings
