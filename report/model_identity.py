
from __future__ import annotations

import glob
import json
import re

from fabric.workspace_context import _load_registry_meta
from util.logger_setup import get_logger

logger = get_logger(__name__)

_CATALOG_RE = re.compile(r'initial catalog="([^"]+)"', re.IGNORECASE)


def report_catalog(report_path: str) -> str:
    if not report_path:
        return ""
    for pbir in glob.glob(f"{report_path}/**/definition.pbir", recursive=True):
        try:
            with open(pbir, encoding="utf-8") as f:
                conn = (
                    json.load(f)
                    .get("datasetReference", {})
                    .get("byConnection", {})
                    .get("connectionString", "")
                )
        except Exception as e:
            logger.debug("unreadable %s: %s", pbir, e)
            continue
        m = _CATALOG_RE.search(conn or "")
        if m:
            return m.group(1)
    return ""


def registry_models() -> set[str]:
    try:
        return set(_load_registry_meta().get("model_names") or [])
    except Exception:
        return set()


def registry_generated_at() -> str:
    try:
        return str(_load_registry_meta().get("generated_at") or "")
    except Exception:
        return ""


def registry_mismatch(report_path: str) -> str | None:
    catalog = report_catalog(report_path)
    models = registry_models()
    if not catalog or not models:
        return None
    if catalog in models:
        return None

    when = registry_generated_at()
    stamp = f", cached {when}" if when else ""
    return (
        f"the field registry describes {sorted(models)}{stamp}, but this report "
        f"is bound to '{catalog}'. Field names validated against it would be "
        f"checked against the wrong model — real fields rejected, missing ones "
        f"accepted. Run pbi_refresh_registry for '{catalog}' before binding."
    )


def registry_is_for(report_path: str) -> bool:
    catalog = report_catalog(report_path)
    return bool(catalog) and catalog in registry_models()


def describe(report_path: str) -> str:
    catalog = report_catalog(report_path) or "(no live connection)"
    models = sorted(registry_models()) or ["(empty)"]
    when = registry_generated_at() or "unknown"
    verdict = "MATCH" if registry_is_for(report_path) else "MISMATCH/UNKNOWN"
    return (
        f"report bound to '{catalog}'; registry built from {models} at {when} "
        f"-> {verdict}"
    )
