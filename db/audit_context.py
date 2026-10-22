from __future__ import annotations

import threading

_DEFAULTS: dict = {
    "user_email": "",
    "ip_address": "",
    "project_id": None,
    "project_name": "",
}

_local = threading.local()


def _ctx() -> dict:
    d = getattr(_local, "ctx", None)
    if d is None:
        d = dict(_DEFAULTS)
        _local.ctx = d
    return d


def set_actor(user_email: str = "", ip_address: str = "") -> None:
    c = _ctx()
    c["user_email"] = user_email or ""
    c["ip_address"] = ip_address or ""


def set_project(project_id=None, project_name: str = "") -> None:
    c = _ctx()
    c["project_id"] = project_id
    c["project_name"] = project_name or ""


def get_context() -> dict:
    return dict(_ctx())


def adopt_context(snapshot: dict) -> None:
    _local.ctx = dict(_DEFAULTS, **(snapshot or {}))
