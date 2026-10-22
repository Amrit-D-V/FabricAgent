"""Active Fabric workspace/model context plus per-model registry access.

Extracted from app.py so non-Streamlit entry points (pbi_mcp_server,
chat_api_server) and the report/llm engines can share the same context
without importing the UI.

FABRIC_WORKSPACE / FABRIC_MODELS resolve once at import (DB row via
get_fabric_connection, then .env fallback); app.py re-resolves per rerun
and pushes the live values back in through set_fabric_context().
"""

import json
import os
import threading

from config import (
    FABRIC_MODEL as _SINGLE_FABRIC_MODEL,
)
from config import (
    FABRIC_MODELS as _CONFIG_FABRIC_MODELS,
)
from config import (
    FABRIC_WORKSPACE,
    PROJECT_ROOT,
)
from fabric.fabric_connection import get_fabric_connection as _get_fc

if all(
    os.getenv(k) for k in ("AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_CLIENT_SECRET")
):
    os.environ.setdefault("AZURE_TOKEN_CREDENTIALS", "prod")

_fc_active = _get_fc()
FABRIC_WORKSPACE = _fc_active.workspace or FABRIC_WORKSPACE
FABRIC_MODELS = list(_fc_active.models) or list(_CONFIG_FABRIC_MODELS)
if not FABRIC_MODELS and _SINGLE_FABRIC_MODEL:
    FABRIC_MODELS = [_SINGLE_FABRIC_MODEL]

_ACTIVE_WF_PALETTE: list[str] | None = None


def set_fabric_context(workspace: str, models) -> None:
    """Adopt the per-rerun workspace/models selection (called by app.py)."""
    global FABRIC_WORKSPACE, FABRIC_MODELS
    FABRIC_WORKSPACE = workspace
    FABRIC_MODELS = list(models)


def set_active_wf_palette(palette: list[str] | None) -> None:
    """Adopt the approved-wireframe palette for this rerun (app.py)."""
    global _ACTIVE_WF_PALETTE
    _ACTIVE_WF_PALETTE = palette


_REGISTRY_JSON_CACHE: dict[str, tuple[float, dict]] = {}


def _read_registry_json(path: str) -> dict:
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    hit = _REGISTRY_JSON_CACHE.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    _REGISTRY_JSON_CACHE[path] = (mtime, data)
    return data


_REGISTRY_DIR = os.path.join(str(PROJECT_ROOT), ".registry_cache")


_workspace_ctx_local = threading.local()


def snapshot_workspace_context() -> dict:
    return {
        "workspace": FABRIC_WORKSPACE,
        "models": tuple(FABRIC_MODELS),
        "palette": _ACTIVE_WF_PALETTE,
    }


def adopt_workspace_context(snapshot: dict) -> None:
    _workspace_ctx_local.ctx = dict(snapshot or {})


def _workspace_ctx_override() -> dict | None:
    return getattr(_workspace_ctx_local, "ctx", None)


def _active_models() -> tuple[str, ...]:
    _ov = _workspace_ctx_override()
    if _ov is not None:
        return tuple(_ov.get("models") or ())
    env_m = os.environ.get("PBI_ACTIVE_MODELS", "")
    if env_m:
        try:
            _m = json.loads(env_m)
            if _m:
                return tuple(_m)
        except Exception:
            pass
    return tuple(FABRIC_MODELS)


def _active_workspace() -> str:
    _ov = _workspace_ctx_override()
    if _ov is not None:
        return _ov.get("workspace") or ""
    env_ws = os.environ.get("PBI_ACTIVE_WORKSPACE", "")
    if env_ws:
        return env_ws
    # Resolve current workspace name dynamically from saved connection if available
    try:
        conn = _get_fc()
        if FABRIC_WORKSPACE == conn.workspace:
            return conn.live_workspace() or FABRIC_WORKSPACE
    except Exception:
        pass
    return FABRIC_WORKSPACE


def _active_palette():
    _ov = _workspace_ctx_override()
    if _ov is not None:
        return _ov.get("palette")
    return _ACTIVE_WF_PALETTE


def _registry_path(kind: str, models: tuple[str, ...] | None = None) -> str:
    import hashlib as _hashlib

    _m = sorted(models if models is not None else _active_models())
    fp = _hashlib.sha1(json.dumps([_active_workspace(), _m]).encode()).hexdigest()[:12]
    return os.path.join(_REGISTRY_DIR, f"{kind}_{fp}.json")


def _read_registry(kind: str) -> dict:
    data = _read_registry_json(_registry_path(kind))
    if not data and _active_models():
        legacy = _read_registry_json(
            os.path.join(str(PROJECT_ROOT), f".{kind}_registry.json")
        )
        _legacy_meta = legacy.get("_meta") or {}
        if (
            sorted(_legacy_meta.get("model_names") or []) == sorted(_active_models())
            and _legacy_meta.get("workspace") == _active_workspace()
        ):
            data = legacy
    return data


def _load_column_registry() -> dict:
    return {k: v for k, v in _read_registry("column").items() if not k.startswith("_")}


def _load_registry_meta() -> dict:
    return _read_registry("measure").get("_meta", {}) or {}


def _load_measure_registry() -> dict:
    return {k: v for k, v in _read_registry("measure").items() if not k.startswith("_")}
