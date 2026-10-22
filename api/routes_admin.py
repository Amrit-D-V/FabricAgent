"""Project settings, skills, and the Fabric connection library.

These were the six sections of pages/12_Project_Management.py. The panels that
drew them (ui/skills_panel.py, ui/fabric_panel.py, ui/source_cards.py) were
Streamlit widgets over functions that already lived in db/ and skills_catalog,
so only the drawing was lost -- the operations are all still here.
"""

from __future__ import annotations

from starlette.requests import Request

from api.registry import json_body, route
from api.sse import stream
from core.context import current_email, state
from core.errors import AppError, NotFound
from db.database import audit


def _project():
    # api.registry has already required one; this resolves it. Both go through
    # auth.auth.require_project so there is a single definition of "selected".
    from auth.auth import require_project

    return require_project()


def _view():
    """One project fetch, the way ProjectView batched it for the page."""
    from core.project_view import load_project_view

    view = load_project_view(_project().id, current_email())
    if not view:
        raise NotFound("That project could not be loaded.")
    return view


# --- project settings --------------------------------------------------------


@route("/project/settings")
def settings(_request: Request):
    view = _view()
    return {
        "id": view.id,
        "name": view.name,
        "description": view.description,
        "documents": [
            {"id": d.id, "name": d.original_name or d.filename, "type": d.file_type}
            for d in view.documents
        ],
        "sources": {kind: view.source(kind) for kind in SOURCE_KINDS},
        "llm": view.metadata.get("llm_config") or {},
    }


@route("/project/settings", methods=("PATCH",), capability="editor")
async def update_settings(request: Request):
    from db.database import update_project

    project = _project()
    body = await json_body(request)
    update_project(
        project.id,
        name=(body.get("name") or project.name).strip(),
        description=(body.get("description") or "").strip(),
        user_email=current_email(),
    )
    from auth.auth import refresh_project_list

    refresh_project_list()
    return {"ok": True}


# --- per-project LLM ---------------------------------------------------------


# What "apply to all agents" applies to. Each row is a real agent key that
# llm_client routes on; the titles are how the page names them.
AGENTS = [
    (
        "semantic_model",
        "Semantic Model Architect & Builder",
        "Schema plans, star-schema optimization, DAX measure creation.",
    ),
    (
        "chat",
        "FabricBI Chat & Modeling Assistant",
        "Live MCP tool execution, interactive model editing, memory updates.",
    ),
    (
        "dax",
        "DAX Assistant & TMDL Doctor",
        "DAX analysis, formula repair, TMDL validation and repairs.",
    ),
    (
        "dashboard",
        "Dashboard & Coverage Validator",
        "Requirement assertions, coverage reports, design judging.",
    ),
]


@route("/project/llm", capability="editor")
def get_llm(_request: Request):
    """The pin, and what every agent resolves to under it."""
    from db.database import get_project_llm_config
    from llm.llm_client import get_model_name, get_provider

    pinned = get_project_llm_config(_project().id) or {}
    # get_provider() applies the active project's pin itself (it reads the
    # project from audit_context), so each row is exactly what that agent
    # will use -- pinned or system default.
    return {
        "provider": pinned.get("provider", ""),
        "model": pinned.get("model", ""),
        "agents": [
            {
                "key": key,
                "title": title,
                "description": description,
                "provider": get_provider(key),
                "model": get_model_name(key),
            }
            for key, title, description in AGENTS
        ],
    }


@route("/project/llm/test", methods=("POST",), capability="editor")
async def test_llm(request: Request):
    """Try a provider/model before pinning it: one tiny completion, timed."""
    body = await json_body(request)
    provider = (body.get("provider") or "").strip()
    model = (body.get("model") or "").strip()
    if not provider or not model:
        raise AppError("Choose a provider and a model to test.")

    def work(emit):
        import time

        from llm.llm_client import friendly_provider_error, stream_text

        emit("progress", {"message": f"Testing {provider} / {model}…"})
        started = time.time()
        try:
            pieces = stream_text(
                "You are a test assistant.",
                "Respond with the single word: OK",
                max_tokens=64,
                agent="config_test",
                provider=provider,
                model=model,
            )
            text = "".join(c for c in pieces if isinstance(c, str)).strip()
        except Exception as exc:
            raise AppError(friendly_provider_error(provider, model, exc)) from exc
        return {"ms": int((time.time() - started) * 1000), "text": text[:200]}

    return stream(work)


@route("/project/llm", methods=("POST",), capability="editor")
async def set_llm(request: Request):
    """Pin every agent in this project to one provider/model, or clear it."""
    from db.database import clear_project_llm_config, set_project_llm_config

    project = _project()
    body = await json_body(request)
    provider = (body.get("provider") or "").strip()
    model = (body.get("model") or "").strip()
    if not provider or not model:
        clear_project_llm_config(project.id, user_email=current_email())
        return {"cleared": True}
    set_project_llm_config(project.id, provider, model, user_email=current_email())
    return {"provider": provider, "model": model}


# --- skills ------------------------------------------------------------------


@route("/skills", project=False)
def list_skills(_request: Request):
    """Every skill, with whether it is on for the active project.

    Built-in skills come from disk, custom ones from the database; the
    per-project override decides which are actually loaded.
    """
    import skills_catalog
    from db.database import get_project_skills, list_custom_skills

    pid = state().get("active_project_id")
    overrides = get_project_skills(pid) if pid else {}
    custom = {s["slug"]: s for s in list_custom_skills(active_only=False)}

    out = []
    for skill in skills_catalog.list_skills():
        slug = skill.get("name") or skill.get("slug", "")
        row = custom.get(slug)
        out.append(
            {
                "slug": slug,
                "name": skill.get("title") or skill.get("name", slug),
                "description": skill.get("description", ""),
                "custom": row is not None,
                "id": row.get("id") if row else None,
                "enabled": overrides.get(slug, True),
            }
        )
    return out


@route("/skills/{slug}", project=False)
def read_skill(request: Request):
    import skills_catalog

    slug = request.path_params["slug"]
    try:
        return {"slug": slug, "body": skills_catalog.load_skill(slug)}
    except Exception as exc:
        raise NotFound(f"No such skill: {exc}") from exc


@route("/skills/{slug}/enabled", methods=("POST",), capability="editor")
async def toggle_skill(request: Request):
    from db.database import set_project_skill_enabled

    project = _project()
    body = await json_body(request)
    set_project_skill_enabled(
        project.id, request.path_params["slug"], bool(body.get("enabled"))
    )
    return {"ok": True}


@route("/skills", methods=("POST",), capability="admin", project=False)
async def create_skill(request: Request):
    import skills_catalog
    from db.database import create_custom_skill

    body = await json_body(request)
    slug = (body.get("slug") or "").strip()
    name = (body.get("name") or "").strip()
    if not slug or not name:
        raise AppError("A skill needs a slug and a name.")
    skill_id = create_custom_skill(
        slug=slug,
        name=name,
        description=(body.get("description") or "").strip(),
        body=body.get("body") or "",
        created_by=current_email(),
    )
    skills_catalog.invalidate_cache()
    return {"id": skill_id}


@route("/skills/{slug}", methods=("DELETE",), capability="admin", project=False)
def remove_skill(request: Request):
    import skills_catalog
    from db.database import delete_custom_skill, list_custom_skills

    slug = request.path_params["slug"]
    row = next(
        (s for s in list_custom_skills(active_only=False) if s["slug"] == slug), None
    )
    if not row:
        raise NotFound("Only custom skills can be deleted.")
    delete_custom_skill(row["id"])
    skills_catalog.invalidate_cache()
    return {"ok": True}


# --- Fabric connections ------------------------------------------------------


@route("/fabric", capability="admin", project=False)
def fabric(_request: Request):
    from db.database import fabric_connection_status, list_fabric_connections

    return {
        "status": fabric_connection_status(),
        "connections": list_fabric_connections(),
    }


@route("/fabric", methods=("POST",), capability="admin", project=False)
async def save_fabric(request: Request):
    """Create or update a connection. An empty secret keeps the stored one.

    The credential must prove itself against Entra before anything is stored.
    An app connection is not scoped to a workspace -- it reaches whatever its
    service principal can see -- so a secret that cannot sign in would be a
    connection that silently does nothing, discovered later and somewhere else.
    """
    from db.database import get_fabric_connection_by_id, save_fabric_connection_entry
    from fabric.admin_user_access import client_credentials_token

    body = await json_body(request)
    name = (body.get("name") or "").strip()
    if not name:
        raise AppError("A connection needs a name.")

    tenant_id = (body.get("tenantId") or "").strip()
    client_id = (body.get("clientId") or "").strip()
    submitted_secret = (body.get("clientSecret") or "").strip()
    conn_id = body.get("id")

    # Retain stored secret when client secret is not supplied on edit
    secret = submitted_secret
    if not secret and conn_id:
        stored = get_fabric_connection_by_id(int(conn_id)) or {}
        secret = stored.get("client_secret") or ""
        tenant_id = tenant_id or (stored.get("tenant_id") or "")
        client_id = client_id or (stored.get("client_id") or "")

    token, err = client_credentials_token(client_id, tenant_id, secret)
    if not token:
        audit(
            "fabric.connection.rejected",
            target=name,
            severity="warning",
            outcome="denied",
            details=err[:200],
        )
        raise AppError(f"Entra refused those credentials — {err}")

    saved_id = save_fabric_connection_entry(
        name=name,
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=submitted_secret,
        workspace_name="",
        workspace_id="",
        models=body.get("models") or [],
        catalogs=body.get("catalogs") or {},
        conn_id=conn_id,
        make_default=bool(body.get("makeDefault")),
        updated_by=current_email(),
    )
    return {"id": saved_id}


@route("/fabric/{conn_id:int}/test", methods=("POST",), capability="admin", project=False)
def test_fabric(request: Request):
    """Ask Entra whether this stored connection can still sign in.

    Saving verifies once, but a secret expires, an app registration is deleted,
    and a tenant setting changes -- none of which announce themselves here.
    They announce themselves in a chat answer that could not read a model, so
    this is the button that says which of the two it was.
    """
    from db.database import get_fabric_connection_by_id
    from fabric.admin_user_access import client_credentials_token

    conn_id = request.path_params["conn_id"]
    stored = get_fabric_connection_by_id(conn_id)
    if not stored:
        raise NotFound("No such connection.")

    token, err = client_credentials_token(
        stored.get("client_id") or "",
        stored.get("tenant_id") or "",
        stored.get("client_secret") or "",
    )
    audit(
        "fabric.connection.tested",
        target=stored.get("name") or str(conn_id),
        outcome="success" if token else "failure",
        severity="info" if token else "warning",
        details="" if token else err[:200],
    )
    if not token:
        raise AppError(f"{stored.get('name') or 'That connection'} cannot sign in — {err}")
    return {"ok": True, **_connection_health(token)}


def _connection_health(token: str) -> dict:
    """What this principal actually is, and what it actually reaches.

    "No workspace to choose" is only reassuring if you can see what the
    principal already reaches, so the workspace list is the answer to the
    question the removed picker used to ask.

    The `roles` claim matters more than it looks: for the read-only admin APIs
    the tenant setting is the grant, and admin-consent Power BI *application*
    permissions break the call rather than enabling it. Empty is the healthy
    state, so it is worth saying so out loud rather than only on failure.
    """
    import time

    import requests

    from config import POWERBI_API_BASE
    from fabric.admin_user_access import token_claims

    claims = token_claims(token)
    roles = claims.get("roles") or []
    health = {
        "appId": claims.get("appid") or "",
        "appName": claims.get("app_displayname") or "",
        "tenantId": claims.get("tid") or "",
        "expiresInSeconds": max(0, int(claims.get("exp", 0) - time.time())),
        "applicationRoles": roles if isinstance(roles, list) else [roles],
        "workspaces": [],
        "workspacesError": "",
    }

    try:
        resp = requests.get(
            f"{POWERBI_API_BASE}/groups",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
    except requests.RequestException as exc:
        health["workspacesError"] = f"could not reach the Power BI API ({exc})"
        return health

    if resp.status_code >= 400:
        # Signing in worked, so this is a narrower problem than a dead
        # credential and must not be reported as one.
        health["workspacesError"] = (
            f"signed in, but listing workspaces returned {resp.status_code}. "
            "Usually the tenant setting 'Service principals can use Power BI "
            "APIs' is off, or this principal is in no workspace yet."
        )
        return health

    try:
        groups = resp.json().get("value") or []
    except ValueError:
        health["workspacesError"] = "the workspace list came back as non-JSON"
        return health

    health["workspaces"] = [
        {"name": g.get("name") or "", "readOnly": bool(g.get("isReadOnly"))}
        for g in groups
    ]
    return health


@route("/fabric/{conn_id:int}/default", methods=("POST",), capability="admin", project=False)
def make_default(request: Request):
    from db.database import set_default_fabric_connection

    set_default_fabric_connection(request.path_params["conn_id"])
    return {"ok": True}


@route("/fabric/{conn_id:int}", methods=("DELETE",), capability="admin", project=False)
def delete_fabric(request: Request):
    from db.database import delete_fabric_connection_entry

    delete_fabric_connection_entry(request.path_params["conn_id"])
    return {"ok": True}


# --- connected sources -------------------------------------------------------

SOURCE_KINDS = ("sql", "semantic_model")
PREVIEW_LIMIT = 20_000


def _source_kind(request: Request) -> str:
    kind = request.path_params["kind"]
    if kind not in SOURCE_KINDS:
        raise NotFound("No such source.")
    return kind


@route("/project/sources", capability="editor")
def sources(_request: Request):
    """Both connectors' state, the captured schema, and the column catalog."""
    from db import graph_memory
    from fabric.fabric_sql import (
        AUTH_AAD_PASSWORD,
        AUTH_AAD_TOKEN,
        AUTH_SERVICE_PRINCIPAL,
        AUTH_SQL,
    )

    view = _view()

    def one(kind: str) -> dict:
        block = view.context_block(kind)
        return {
            # Secrets withheld: `password_set` says one exists, the value
            # itself never travels to the browser.
            "config": view.source(kind),
            "captured": bool(block),
            "preview": block[:PREVIEW_LIMIT],
            "length": len(block),
        }

    # What the extraction will read, in the words the page shows.
    feeding = []
    if view.documents:
        feeding.append(f"{len(view.documents)} document(s)")
    if view.connected("sql"):
        feeding.append("database schema")
    if view.connected("semantic_model"):
        feeding.append("existing semantic model")
    if graph_memory.enabled():
        feeding.append("org memory")

    return {
        "sources": {kind: one(kind) for kind in SOURCE_KINDS},
        "columns": view.column_catalog(),
        "authModes": [AUTH_AAD_TOKEN, AUTH_AAD_PASSWORD, AUTH_SQL, AUTH_SERVICE_PRINCIPAL],
        "feeding": feeding,
    }


@route("/project/sources/{kind}", methods=("POST",), capability="editor")
async def connect_source(request: Request):
    """Probe first, save second.

    A bad address is never stored, and the address stored is the one that
    actually connected -- a pasted portal URL comes back as the powerbi://
    endpoint. Streamed, because reading a schema can take a while.
    """
    project = _project()
    kind = _source_kind(request)
    email = current_email()
    body = await json_body(request)
    fields = {
        k: (body.get(k) or "").strip()
        for k in ("server", "database", "auth_mode", "username")
    }
    typed_password = (body.get("password") or "").strip()
    profile = bool(body.get("profile", True))
    if kind == "sql":
        from fabric.fabric_sql import AUTH_AAD_TOKEN, normalize_auth_mode

        try:
            fields["auth_mode"] = normalize_auth_mode(fields["auth_mode"] or AUTH_AAD_TOKEN)
        except ValueError as exc:
            raise AppError(str(exc)) from exc
    if not fields["server"] or not fields["database"]:
        raise AppError(
            "Server and database are both required."
            if kind == "sql"
            else "XMLA endpoint and model name are both required."
        )

    def work(emit):
        import json

        from db import graph_memory
        from db.database import (
            COLUMN_CATALOG_SUFFIX,
            get_project_source,
            set_project_context_block,
            set_project_source,
        )
        from model.project_sources import probe_semantic_model, probe_sql_source

        if kind == "sql":
            from fabric.fabric_sql import AUTH_AAD_TOKEN

            emit("progress", {"message": "Reading schema (read-only)…"})
            password = typed_password
            if not password:
                # Blank means "keep the saved one". It is read here, at the
                # moment a connection is opened, and never handed to the form.
                stored = get_project_source(project.id, kind, include_secrets=True)
                password = (stored or {}).get("password", "")
            # Resolved here so the stored config names a real mode, never "".
            fields["auth_mode"] = fields["auth_mode"] or AUTH_AAD_TOKEN
            probe = probe_sql_source(
                fields["server"],
                fields["database"],
                fields["auth_mode"],
                fields["username"],
                password,
                profile=profile,
            )
            config = dict(fields)
            if typed_password:
                # Sent only when one was typed: omitted, set_project_source
                # keeps the stored credential instead of blanking it.
                config["password"] = typed_password
        else:
            emit("progress", {"message": "Reading model metadata (read-only)…"})
            probe = probe_semantic_model(fields["server"], fields["database"])
            config = {"server": fields["server"], "database": fields["database"]}
        if not probe.ok:
            raise AppError(probe.error)

        config["server"] = probe.server or fields["server"]
        emit("progress", {"message": f"Connected — {probe.summary}. Saving…"})
        set_project_source(project.id, kind, config, user_email=email)
        set_project_context_block(project.id, kind, probe.block)
        try:
            set_project_context_block(
                project.id, f"{kind}{COLUMN_CATALOG_SUFFIX}", json.dumps(probe.tables or [])
            )
        except Exception:
            pass  # the catalog only powers the picker -- never block a connection
        try:
            graph_memory.remember_source(
                project.id,
                kind,
                config["server"],
                config["database"],
                probe.tables,
                project_name=project.name,
            )
        except Exception:
            pass  # org memory is best-effort -- never block a connection on it
        return {"summary": probe.summary, "server": config["server"]}

    return stream(work)


@route("/project/sources/{kind}", methods=("DELETE",), capability="editor")
def disconnect_source(request: Request):
    """Drops the config, the captured schema, its column catalog and any stored
    credential. Requirements already extracted are kept."""
    from db.database import clear_project_source

    clear_project_source(_project().id, _source_kind(request), user_email=current_email())
    return {"ok": True}


# --- requirements ------------------------------------------------------------


@route("/project/requirements", capability="editor")
def get_requirements(_request: Request):
    from db.database import get_project_requirements, requirements_revision
    from model.project_requirements import open_questions, parse_spec

    project = _project()
    text = get_project_requirements(project.id) or ""
    return {
        "text": text,
        "revision": requirements_revision(text),
        "spec": parse_spec(text) if text else {},
        "openQuestions": [
            {"question": key, "text": shown}
            for key, shown in (open_questions(text) if text else [])
        ],
    }


@route("/project/requirements", methods=("PUT",), capability="editor")
async def put_requirements(request: Request):
    """Save an edited spec, refusing a write made against a stale revision."""
    from db.database import StaleWriteError, set_project_requirements

    project = _project()
    body = await json_body(request)
    try:
        revision = set_project_requirements(
            project.id,
            body.get("text") or "",
            expected_rev=body.get("revision") or None,
        )
    except StaleWriteError:
        raise AppError(
            "Someone else changed the requirements while you were editing. "
            "Reload to pick up their version before saving."
        ) from None
    return {"revision": revision}


@route("/project/requirements/questions", methods=("POST",), capability="editor")
async def clarifying_questions(_request: Request):
    """What the sources leave open — answered before the spec is written."""
    from model.project_requirements import ask_clarifying_questions

    project = _project()

    def work(emit):
        emit("progress", {"message": "Reading the project's sources…"})
        questions = ask_clarifying_questions(project.id)
        emit("progress", {"message": f"{len(questions)} question(s) worth settling."})
        return {"questions": questions}

    return stream(work)


@route("/project/requirements/extract", methods=("POST",), capability="editor")
async def extract_requirements(request: Request):
    """Write the spec. Slow enough to stream: it reads every source and calls
    the project's LLM."""
    body = await json_body(request)
    project = _project()
    email = current_email()  # the worker thread has no request scope to read
    answers = body.get("answers") or None

    def work(emit):
        from db.database import audit
        from model.project_requirements import extract_project_requirements

        emit("progress", {"message": "Reading sources and writing the spec…"})
        result = extract_project_requirements(
            project.id, project_name=project.name, answers=answers
        )
        if result.text:
            try:
                audit(
                    "requirements.extract",
                    target=project.name,
                    details=", ".join(result.sources),
                    user_email=email,
                    project_id=project.id,
                )
            except Exception:
                pass  # never lose a good extraction to an audit failure
        return {
            "text": result.text,
            "read": result.read,
            "sources": result.sources,
            "images": result.images,
            "truncated": result.truncated,
            "skipped": result.skipped,
            "graphed": result.graphed,
        }

    return stream(work)


def _rewrite_spec(rewrite, revision: str | None, action: str, details: str) -> dict:
    """Read the spec, rewrite one line of it, write it back.

    Guarded by the revision the caller read it at, so two people resolving
    different questions cannot overwrite each other. Every single-line edit
    (answer, dismiss, bind) goes through here.
    """
    from db.database import (
        StaleWriteError,
        audit,
        get_project_requirements,
        set_project_requirements,
    )

    project = _project()
    text = get_project_requirements(project.id) or ""
    updated = rewrite(text)
    if updated == text:
        # The line could not be located. Saying so beats writing the text
        # back unchanged, which reads as a dead button.
        raise AppError(
            "Could not find that line in the spec — it may have been edited "
            "since this page loaded. Reload and try again."
        )
    try:
        new_revision = set_project_requirements(
            project.id, updated, expected_rev=revision or None
        )
    except StaleWriteError:
        raise AppError("The requirements changed while you were editing — reload.") from None
    try:
        audit(
            action,
            target=project.name,
            details=details[:200],
            user_email=current_email(),
            project_id=project.id,
        )
    except Exception:
        pass  # never lose a good write to an audit failure
    return {"text": updated, "revision": new_revision}


@route("/project/requirements/resolve", methods=("POST",), capability="editor")
async def resolve_question(request: Request):
    """Answer one open question, folding the decision into the spec. With no
    answer the question is dismissed instead."""
    from model.project_requirements import resolve_open_question

    body = await json_body(request)
    question = (body.get("question") or "").strip()
    if not question:
        raise AppError("Which question?")
    answer = (body.get("answer") or "").strip()
    return _rewrite_spec(
        lambda text: resolve_open_question(text, question, answer),
        body.get("revision"),
        "requirements.question.resolve" if answer else "requirements.question.dismiss",
        question,
    )


@route("/project/requirements/bind", methods=("POST",), capability="editor")
async def bind_kpi(request: Request):
    """Give one unbacked KPI a backing column, rewriting its line in place."""
    from model.project_requirements import bind_kpi as _bind

    body = await json_body(request)
    kpi = (body.get("kpi") or "").strip()
    column = (body.get("column") or "").strip()
    if not kpi or not column:
        raise AppError("Which KPI, and which column?")
    return _rewrite_spec(
        lambda text: _bind(text, kpi, column),
        body.get("revision"),
        "requirements.kpi.bind",
        f"{kpi} -> {column}",
    )


@route("/project/requirements/suggest", methods=("POST",), capability="editor")
async def suggest_bindings(_request: Request):
    """Ask the project's LLM to match each unbacked KPI to a captured column.

    Suggestions only: nothing is written until Bind, and a column the model
    invents is dropped before it gets here.
    """
    from model.project_requirements import parse_spec, suggest_kpi_bindings

    view = _view()
    unbacked = [k for k in parse_spec(view.requirements)["kpis"] if not k["backing"]]
    columns = view.column_catalog()
    if not columns:
        raise AppError(
            "Connect a database or semantic model first — the picker needs its columns."
        )

    def work(emit):
        emit(
            "progress",
            {"message": f"Matching {len(unbacked)} KPI(s) against {len(columns)} column(s)…"},
        )
        return {"matches": suggest_kpi_bindings(unbacked, columns)}

    return stream(work)
