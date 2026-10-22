"""Permissions shared by in-process tools and the chat MCP subprocess.

The API supplies caller values through the child's environment, never through
model-controlled tool arguments. A standalone operator CLI has no web caller.
"""

from __future__ import annotations

import os

from core.context import current_user, state

# Default-deny for readers: adding a new tool must not silently grant writes.
READ_ONLY_TOOLS = frozenset(
    {
        "render_chart",
        "render_map",
        "pbi_layout_compute",
        "pbi_report_info",
        "pbi_visual_list",
        "pbi_visual_bindings",
        "pbi_report_inspect",
        "pbi_desktop_status",
        "pbi_model_inspect",
        "pbi_dax_query",
        "pbi_workspace_items",
        "pbi_user_access",
        "pbi_paginated_get",
        "pbi_paginated_validate",
        "pbi_paginated_list",
    }
)


def has_web_caller() -> bool:
    return current_user() is not None or any(
        key in os.environ
        for key in (
            "FABRICBI_CALLER_EMAIL",
            "FABRICBI_CALLER_ROLE",
            "FABRICBI_CALLER_PROJECT",
        )
    )


def read_only() -> bool:
    if os.getenv("FABRICBI_CALLER_READ_ONLY") == "1":
        return True
    if current_user() is not None:
        from auth.auth import effective_role

        return effective_role() not in {"editor", "admin"}
    role = (os.getenv("FABRICBI_CALLER_ROLE") or "").lower()
    return has_web_caller() and role not in {
        "editor",
        "admin",
    }


def tool_allowed(name: str) -> bool:
    return not read_only() or name in READ_ONLY_TOOLS


# Workspace names can contain commas, so the caller's list is passed to the MCP
# child separated by the ASCII unit separator instead.
WORKSPACE_SEP = "\x1f"
WORKSPACE_ENV = "FABRICBI_CALLER_WORKSPACES"


def allowed_workspaces() -> set | None:
    """Ids and names this caller may reach, lowercased. None means no gate.

    The tools run as FabricBI's service principal, which can see workspaces the
    person chatting cannot. Without this, "show me what is in <workspace>" is
    answered with the SP's reach rather than the caller's -- signing in to
    FabricBI would quietly borrow its access.

    None and an empty set mean opposite things: None is "no list was supplied,
    so there is nothing to enforce" (gating off, or an operator CLI), while an
    empty set is "this caller reaches nothing" and denies everything.
    """
    if current_user() is not None:
        from auth.workspace_access import for_user

        access = for_user((current_user() or {}).get("email", ""))
        if access.status == "off":
            return None
        if not access.usable:
            return set()  # fail closed: unknown access is not access
        out: set = set()
        for wid, meta in access.workspaces.items():
            if wid:
                out.add(wid)
            name = (meta.get("name") or "").strip().lower()
            if name:
                out.add(name)
        return out

    raw = os.getenv(WORKSPACE_ENV)
    if raw is None:
        return None
    return {p.strip().lower() for p in raw.split(WORKSPACE_SEP) if p.strip()}


def workspace_refusal(workspace: str | None) -> str:
    """A message when this caller may not touch `workspace`, else "".

    Only an explicitly named workspace is checked. When a tool names none it
    falls back to the active project's workspace, and that is already governed
    by the project gate in db.database._apply_fabric_gate -- checking it again
    here would refuse on a value the caller never chose.
    """
    wanted = (workspace or "").strip().lower()
    if not wanted:
        return ""
    allowed = allowed_workspaces()
    if allowed is None or wanted in allowed:
        return ""
    return (
        f"REFUSED — you do not have access to the '{workspace}' workspace in "
        "Fabric. FabricBI acts with its own service principal, which can reach "
        "more than you can, so it will not read a workspace on your behalf that "
        "you could not open yourself. Ask a workspace admin for access in "
        "Fabric, then try again."
    )


def authorized_project() -> str | None:
    """None for the operator CLI; empty for a web caller without a project."""
    if current_user() is not None:
        from auth.auth import visible_projects

        pid = state().get("active_project_id")
        project = next((p for p in visible_projects() if p.id == pid), None)
        return project.name if project else ""
    if has_web_caller():
        return os.getenv("FABRICBI_CALLER_PROJECT", "")
    return None
