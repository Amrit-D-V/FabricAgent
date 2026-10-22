from __future__ import annotations

from datetime import datetime

import requests

from config import (
    FABRIC_ACCESS_ALLOW_STALE,
    FABRIC_ACCESS_GATE,
    FABRIC_ACCESS_TTL_S,
    POWERBI_API_BASE,
)
from util.logger_setup import get_logger

logger = get_logger(__name__)

# What the gate decided, and why. `reason` is shown to the user, so it says what
# to do about it rather than restating the rule.
FRESH, STALE, UNAVAILABLE, OFF = "fresh", "stale", "unavailable", "off"


class WorkspaceAccess:
    def __init__(self, workspaces: dict, status: str, reason: str = ""):
        self.workspaces = workspaces  # {workspace_id: {name, is_read_only}}
        self.status = status
        self.reason = reason

    @property
    def usable(self) -> bool:
        return self.status in (FRESH, STALE, OFF)

    def can_see(self, workspace_id: str) -> bool:
        if self.status == OFF:
            return True
        if not self.usable:
            return False
        return (workspace_id or "").strip().lower() in self.workspaces

    def is_read_only(self, workspace_id: str) -> bool:

        if self.status == OFF:
            return False
        if not self.usable:
            return True
        entry = self.workspaces.get((workspace_id or "").strip().lower())
        return True if entry is None else bool(entry.get("is_read_only"))


def _fetch_workspaces(token: str) -> list:
    """Raw /groups for the caller. Raises on transport or HTTP failure."""
    resp = requests.get(
        f"{POWERBI_API_BASE}/groups",
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json().get("value", []) or []


def _age_seconds(checked_at: str) -> float:
    try:
        return (datetime.now() - datetime.fromisoformat(checked_at)).total_seconds()
    except Exception:
        return float("inf")


def _cached(email: str) -> tuple[dict, float]:
    from db.database import get_user_workspace_access

    raw = get_user_workspace_access(email)
    if not raw:
        return {}, float("inf")
    age = min(_age_seconds(v.get("checked_at", "")) for v in raw.values())
    return (
        {
            (wid or "").strip().lower(): {
                "name": v.get("name", ""),
                "is_read_only": bool(v.get("is_read_only")),
            }
            for wid, v in raw.items()
        },
        age,
    )


def refresh(email: str) -> WorkspaceAccess:
    """Re-read the user's workspaces from Power BI and cache the result."""
    from auth import entra_sso
    from db.database import save_user_workspace_access

    token, err = entra_sso.acquire_powerbi_token(email)
    if not token:
        logger.warning("workspace access refresh failed for %s: %s", email, err)
        return WorkspaceAccess({}, UNAVAILABLE, err or "could not reach Power BI")

    try:
        raw = _fetch_workspaces(token)
    except Exception as exc:
        logger.warning("Power BI /groups failed for %s: %s", email, exc)
        return WorkspaceAccess({}, UNAVAILABLE, f"Power BI did not respond ({exc})")

    try:
        save_user_workspace_access(email, raw)
    except Exception as exc:  # caching is an optimisation, not the answer
        logger.warning("could not cache workspace access for %s: %s", email, exc)

    return WorkspaceAccess(
        {
            (w.get("id") or "").strip().lower(): {
                "name": w.get("name", ""),
                "is_read_only": bool(w.get("isReadOnly")),
            }
            for w in raw
        },
        FRESH,
    )


def for_user(email: str, force: bool = False) -> WorkspaceAccess:
    if not FABRIC_ACCESS_GATE:
        return WorkspaceAccess({}, OFF, "workspace gating is disabled")

    email = (email or "").lower().strip()
    if not email:
        return WorkspaceAccess({}, UNAVAILABLE, "no signed-in user")

    if not force:
        cached, age = _cached(email)
        if cached and age < FABRIC_ACCESS_TTL_S:
            return WorkspaceAccess(cached, FRESH)

    fresh = refresh(email)
    if fresh.status == FRESH:
        return fresh

    # Refresh failed. Serving a stale list is a deliberate, opt-in trade:
    # correct during a Power BI outage, wrong the moment someone's access is
    # revoked. Off by default.
    cached, age = _cached(email)
    if cached and FABRIC_ACCESS_ALLOW_STALE:
        mins = int(age // 60)
        return WorkspaceAccess(
            cached,
            STALE,
            f"Power BI is unreachable; using access last confirmed {mins} min ago.",
        )
    return fresh


def visible_workspace_ids(email: str) -> set:
    return set(for_user(email).workspaces)
