"""Projects, users and the audit log.

Thin wrappers: every one of these already existed as a function in db/ that the
Streamlit pages called directly. What the API adds is the authorization that
used to be spread across the pages -- see the `capability` argument on each
route, and auth.visible_projects for why listing goes through one place.
"""

from __future__ import annotations

from starlette.requests import Request

from api.registry import json_body, route
from auth.auth import refresh_project_list, visible_projects
from core.context import current_user, is_admin
from core.errors import AccessDenied, NotFound


def _as_dict(project) -> dict:
    return {
        "id": project.id,
        "name": project.name,
        "description": getattr(project, "description", "") or "",
        "clientId": getattr(project, "client_id", None),
        "status": getattr(project, "status", "") or "",
    }


@route("/projects", project=False)
def list_projects(_request: Request):
    return [_as_dict(p) for p in visible_projects()]


@route("/projects", methods=("POST",), capability="editor", project=False)
async def create_project(request: Request):
    from db.database import create_project as _create, get_or_create_default_client

    body = await json_body(request)
    name = (body.get("name") or "").strip()
    if not name:
        raise NotFound("A project needs a name.")
    client_id = body.get("clientId") or get_or_create_default_client().id
    project = _create(
        name=name,
        client_id=client_id,
        description=(body.get("description") or "").strip(),
        user_email=(current_user() or {}).get("email", ""),
    )
    refresh_project_list()
    return _as_dict(project)


@route("/projects/{project_id:int}", project=False)
def get_project(request: Request):
    pid = request.path_params["project_id"]
    _assert_visible(pid)
    from db.database import get_project as _get

    project = _get(pid)
    if not project:
        raise NotFound("No such project.")
    return _as_dict(project)


@route("/projects/{project_id:int}", methods=("DELETE",), capability="admin", project=False)
def delete_project(request: Request):
    pid = request.path_params["project_id"]
    _assert_visible(pid)
    from db.database import delete_project as _delete

    message = _delete(pid, user_email=(current_user() or {}).get("email", ""))
    refresh_project_list()
    return {"message": message}


def _assert_visible(project_id: int) -> None:
    """Membership is the gate -- never trust an id from the client."""
    if not any(p.id == project_id for p in visible_projects()):
        raise AccessDenied("You do not have access to that project.")


# --- users -----------------------------------------------------------------


@route("/users", capability="admin", project=False)
def list_users(_request: Request):
    from db.database import list_app_users

    return list_app_users()


@route("/users", methods=("POST",), capability="admin", project=False)
async def create_user(request: Request):
    from db.database import create_app_user

    body = await json_body(request)
    email = (body.get("email") or "").strip().lower()
    if not email:
        raise NotFound("An account needs an email address.")
    # No password: sign-in is Microsoft-only, so app_users holds no credential.
    create_app_user(email, password_hash="", role=body.get("role") or "viewer")
    return {"ok": True}


@route("/users/{email}", methods=("PATCH",), capability="admin", project=False)
async def update_user(request: Request):
    from db.database import count_active_admins, set_user_active, update_user_role

    email = request.path_params["email"].lower()
    body = await json_body(request)

    if "role" in body or "active" in body:
        _guard_last_admin(email, body, count_active_admins)
    if "role" in body:
        update_user_role(email, body["role"])
    if "active" in body:
        set_user_active(email, bool(body["active"]))
    return {"ok": True}


def _guard_last_admin(email: str, body: dict, count_active_admins) -> None:
    """Locking out the last admin would need database access to undo."""
    demoting = body.get("role") not in (None, "admin")
    disabling = body.get("active") is False
    if not (demoting or disabling):
        return
    from db.database import get_app_user

    rec = get_app_user(email) or {}
    if rec.get("role") == "admin" and count_active_admins() <= 1:
        raise AccessDenied("That is the last active admin -- promote another first.")


@route("/users/{email}", methods=("DELETE",), capability="admin", project=False)
def delete_user(request: Request):
    from db.database import count_active_admins, delete_app_user

    email = request.path_params["email"].lower()
    _guard_last_admin(email, {"active": False}, count_active_admins)
    if email == (current_user() or {}).get("email", ""):
        raise AccessDenied("You cannot delete your own account.")
    delete_app_user(email)
    return {"ok": True}


# --- audit log --------------------------------------------------------------


@route("/audit", capability="admin", project=False)
def audit_log(request: Request):
    from db.database import (
        distinct_audit_event_types,
        distinct_audit_projects,
        distinct_audit_users,
        list_audit_log,
    )

    from datetime import datetime, timedelta, timezone

    q = request.query_params

    # Stored timestamps are naive UTC, so the cutoff is built the same way --
    # tzinfo stripped from an aware now() rather than the deprecated utcnow().
    # Deriving it from the browser's clock instead would be wrong by the
    # viewer's offset, and wrong in a way that silently hides recent rows.
    windows = {"24h": 1, "7d": 7, "30d": 30}
    days = windows.get(q.get("window") or "")
    since = ""
    if days:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        since = (now - timedelta(days=days)).isoformat()

    entries = list_audit_log(
        user_email=q.get("user") or "",
        event_type=q.get("event") or "",
        severity=q.get("severity") or "",
        since=since,
        limit=min(int(q.get("limit") or 200), 1000),
    )
    return {
        "entries": entries,
        "users": distinct_audit_users(),
        "events": distinct_audit_event_types(),
        "projects": distinct_audit_projects(),
    }


# --- LLM configuration ------------------------------------------------------


@route("/llm/options", project=False)
def llm_options(_request: Request):
    """Catalogue + live model lists -- what render_llm_picker used to draw."""
    from llm.llm_picker import picker_options

    return picker_options()


@route("/llm/keys", capability="admin", project=False)
def llm_key_status(_request: Request):
    from db.database import org_llm_key_status

    return org_llm_key_status()


@route("/llm/keys", methods=("POST",), capability="admin", project=False)
async def set_llm_key(request: Request):
    from db.database import clear_org_llm_key, set_org_llm_key

    body = await json_body(request)
    provider = (body.get("provider") or "").strip()
    key = (body.get("key") or "").strip()
    if not provider:
        raise NotFound("Which provider?")
    email = (current_user() or {}).get("email", "")
    if key:
        set_org_llm_key(provider, key, updated_by=email)
    else:
        clear_org_llm_key(provider)
    return {"ok": True}


@route("/me/capabilities", project=False)
def capabilities(_request: Request):
    """What the current user may do *in the active project*.

    The UI hides controls with this; the routes still enforce it. The Fabric
    workspace cap means this can be lower than the app_users role.
    """
    from auth.auth import active_workspace_id, effective_role
    from core.context import current_role

    return {
        "role": current_role(),
        "effectiveRole": effective_role(),
        "workspaceId": active_workspace_id(),
        "isAdmin": is_admin(),
    }


@route("/pipeline")
def pipeline(_request: Request):
    """End-to-end status for the active project, and where to go next.

    Four stages, each derived rather than stored, so the answer cannot drift
    from what the project actually contains.
    """
    from db.database import list_documents, list_semantic_models
    from auth.auth import require_project

    project = require_project()

    docs = list_documents(project.id)
    all_models = list_semantic_models(project.id)
    models = [m for m in all_models if m.tmdl_files]
    latest = models[0] if models else None
    shipped = any(m.pbip_path for m in all_models)

    stages = [
        {
            "n": 1,
            "title": "Requirements & Documents",
            "done": len(docs) > 0,
            "detail": f"{len(docs)} doc(s) attached",
            "path": "/documents",
            "cta": "Manage Documents",
        },
        {
            "n": 2,
            "title": "Semantic Model",
            "done": latest is not None,
            "detail": f"{latest.model_name} built" if latest else "no model built",
            "path": "/semantic-model",
            "cta": "Open Semantic Model Builder",
        },
        {
            "n": 3,
            "title": "Coverage check",
            "done": latest is not None,
            "detail": "model verified" if latest else "needs model",
            "path": "/reports",
            "cta": "Open Reports",
        },
        {
            "n": 4,
            "title": "Ship (export / deploy)",
            "done": shipped,
            "detail": "exported to .pbip / deployed" if shipped else "not exported yet",
            "path": "/reports",
            "cta": "Open Reports",
        },
    ]
    return {
        "project": {"id": project.id, "name": project.name},
        "stages": stages,
        "done": sum(1 for s in stages if s["done"]),
    }
