from __future__ import annotations

from starlette.requests import Request

from api.registry import json_body, route
from api.sse import stream
from auth.auth import visible_projects
from core.context import state
from core.errors import AccessDenied, AppError, NotFound

AUTH_MODES = [
    "Service Principal (connection string)",
    "SQL Username + Password",
    "AAD Username + Password (your Fabric login)",
    "Token (your Fabric login)",
]


def _project():
    # api.registry has already required one; this resolves it. Both go through
    # auth.auth.require_project so there is a single definition of "selected".
    from auth.auth import require_project

    return require_project()


@route("/builder/options", capability="editor")
def options(_request: Request):
    """What the first step offers, plus whatever the project already knows."""
    from db.database import get_project_requirements, list_fabric_connections

    project = _project()
    proj_reqs = get_project_requirements(project.id) or ""
    return {
        "authModes": AUTH_MODES,
        "connections": [
            {
                "id": c.get("id"),
                "name": c.get("name"),
                # No workspace: an app connection reaches everything its
                # service principal can see, so the principal is what
                # distinguishes one from another.
                "clientId": c.get("client_id") or "",
            }
            for c in list_fabric_connections()
        ],
        "project": {"id": project.id, "name": project.name},
        "projectRequirements": proj_reqs,
    }



@route("/builder/connect", methods=("POST",), capability="editor")
async def connect(request: Request):
    """Step 1 — read the warehouse schema, and optionally profile it."""
    from fabric.warehouse_schema import _parse_schema_ddl, fetch_schema

    body = await json_body(request)
    server = (body.get("server") or "").strip()
    database = (body.get("database") or "").strip()
    if not server or not database:
        raise AppError("A server and a database are required.")
    auth_mode = body.get("authMode") or AUTH_MODES[0]

    def work(emit):
        emit("progress", {"message": f"Connecting to {database} on {server}…"})
        schema_text = fetch_schema(
            server,
            database,
            auth_mode,
            username=body.get("username") or "",
            password=body.get("password") or "",
        )
        tables = _parse_schema_ddl(schema_text)
        emit("progress", {"message": f"Read {len(tables)} table(s)."})

        profile = {}
        profile_context = ""
        if body.get("profile"):
            emit("progress", {"message": "Profiling live data…"})
            try:
                from fabric.warehouse_profile import _profile_data, _profile_to_context

                profile = _profile_data(
                    server,
                    database,
                    auth_mode,
                    schema_text,
                    username=body.get("username") or "",
                    password=body.get("password") or "",
                )
                profile_context = _profile_to_context(profile, schema_text=schema_text)
            except Exception as exc:  # profiling is optional grounding
                emit("progress", {"message": f"Profiling skipped: {exc}"})

        return {
            "schemaText": schema_text,
            "tables": {name: cols for name, cols in tables.items()},
            "profile": profile,
            "profileContext": profile_context,
        }

    return stream(work)


@route("/builder/requirements", methods=("POST",), capability="editor")
async def requirements(request: Request):
    """Step 2 — turn the project's documents into a requirements brief."""
    body = await json_body(request)
    project = _project()

    def work(emit):
        from db.database import (
            get_project_requirements,
            list_documents,
            read_document_text,
        )
        from semantic_model_v2.requirements_extract import extract_document_requirements

        emit("progress", {"message": "Reading project documents…"})
        chunks = []
        proj_reqs = get_project_requirements(project.id) or ""
        if proj_reqs:
            chunks.append(
                f"### Authoritative Requirements (Project Management)\n{proj_reqs}"
            )

        for doc in list_documents(project.id):
            try:
                text = read_document_text(doc) or ""
            except Exception:
                continue
            if text:
                chunks.append(f"### {doc.original_name}\n{text}")

        context = "\n\n".join(chunks)
        schema_text = body.get("schemaText") or ""
        if schema_text:
            context += "\n\n=== WAREHOUSE SCHEMA ===\n" + schema_text
        profile_context = body.get("profileContext") or ""
        if profile_context:
            context += "\n\n=== LIVE WAREHOUSE DATA ===\n" + profile_context

        if not context.strip():
            raise AppError(
                "No documents attached and no schema read — there is nothing to "
                "derive requirements from."
            )

        extracted = extract_document_requirements(
            context,
            project_id=project.id,
            progress_callback=lambda m: emit("progress", {"message": m}),
        )
        return {"requirements": extracted}

    return stream(work)


def _pipeline_kwargs(body: dict, project) -> dict:
    from db.database import get_project_requirements
    from semantic_model_v2.requirements_extract import project_rules_context

    reqs = (body.get("requirements") or "").strip()
    if not reqs:
        reqs = get_project_requirements(project.id) or ""

    return {
        "schema_text": body.get("schemaText") or "",
        "schema_tables": {
            name: [tuple(col) for col in cols]
            for name, cols in (body.get("tables") or {}).items()
        },
        "profile": body.get("profile") or None,
        "profile_context": body.get("profileContext") or "",
        "requirements": reqs,
        "model_name": (body.get("modelName") or project.name or "model").strip(),
        "server_fqdn": body.get("server") or "",
        "db_name": body.get("database") or "",
        "rules_context": project_rules_context(project.id),
        "project_id": project.id,
        "kpi_requirements_block": body.get("kpiBlock") or "",
        "reference_model_block": body.get("referenceBlock") or "",
    }



def _result_json(result) -> dict:
    return {
        "planDict": result.plan_dict,
        "tmdlFiles": result.tmdl_files,
        "phaseLog": result.phase_log,
        "succeeded": result.succeeded,
        "errorMessage": result.error_message,
        "readyToDeploy": result.ready_to_deploy,
        "blockingIssues": result.blocking_issues,
        "businessErrors": result.business_errors,
        "measureFailures": [list(f) for f in result.measure_failures],
        "verifyRounds": result.verify_rounds,
        "verifyIssues": result.verify_issues,
        "verifyActions": result.verify_actions,
    }


@route("/builder/plan", methods=("POST",), capability="editor")
async def plan(request: Request):
    """Step 3 — a structured plan the user confirms before any TMDL is written."""
    body = await json_body(request)
    project = _project()

    def work(emit):
        import model

        result = model.generate_model_v2(
            **_pipeline_kwargs(body, project),
            approved_plan=None,
            progress_callback=lambda m: emit("progress", {"message": m}),
            verify_loop_enabled=False,
        )
        return _result_json(result)

    return stream(work)


@route("/builder/generate", methods=("POST",), capability="editor")
async def generate(request: Request):
    """Step 4 — build TMDL from the confirmed plan."""
    body = await json_body(request)
    project = _project()

    def work(emit):
        import model

        approved = None
        raw_plan = body.get("approvedPlan")
        if raw_plan:
            try:
                approved = model.dict_to_plan(raw_plan)
            except Exception:
                # An unparseable plan means re-planning, not a crash.
                emit(
                    "progress",
                    {"message": "Confirmed plan could not be reused — replanning."},
                )

        result = model.generate_model_v2(
            **_pipeline_kwargs(body, project),
            approved_plan=approved,
            progress_callback=lambda m: emit("progress", {"message": m}),
            verify_loop_enabled=bool(body.get("verify", True)),
        )
        return _result_json(result)

    return stream(work)


@route("/builder/generate-background", methods=("POST",), capability="editor")
async def generate_background(request: Request):
    """Step 4 dispatched as an async background job stored in Fabric SQL dbo.background_jobs."""
    body = await json_body(request)
    project = _project()
    from core.jobs import submit_background_job

    approved = None
    raw_plan = body.get("approvedPlan")
    if raw_plan:
        import model

        try:
            approved = model.dict_to_plan(raw_plan)
        except Exception:
            pass

    pipeline_args = _pipeline_kwargs(body, project)
    verify = bool(body.get("verify", True))

    def _execute(progress_callback):
        import model

        progress_callback("Compiling model from approved plan...")
        res = model.generate_model_v2(
            **pipeline_args,
            approved_plan=approved,
            progress_callback=progress_callback,
            verify_loop_enabled=verify,
        )
        return _result_json(res)

    job_id = submit_background_job("model_generation", _execute, project_id=project.id)
    return {"jobId": job_id, "status": "queued"}


@route("/builder/save", methods=("POST",), capability="editor")

async def save(request: Request):
    """Step 5 — persist the generated model against the project."""
    from db.database import save_semantic_model

    body = await json_body(request)
    project = _project()
    tmdl = body.get("tmdlFiles") or {}
    if not tmdl:
        raise AppError("Nothing to save — generate a model first.")

    saved = save_semantic_model(
        project_id=project.id,
        model_name=(body.get("modelName") or project.name or "model").strip(),
        tmdl_files=tmdl,
        wireframe_id=body.get("wireframeId"),
        server_fqdn=body.get("server") or "",
        db_name=body.get("database") or "",
    )
    return {"id": saved.id, "name": saved.model_name}


# --- project memory ----------------------------------------------------------


@route("/builder/rules", capability="editor")
def list_rules(_request: Request):
    from db.database import list_project_rules

    return list_project_rules(_project().id, active_only=True)


@route("/builder/rules", methods=("POST",), capability="editor")
async def add_rule(request: Request):
    """A rule learned in one run, applied to every later one."""
    from db.database import save_project_rule

    body = await json_body(request)
    text = (body.get("text") or "").strip()
    if not text:
        raise AppError("A rule needs some text.")
    save_project_rule(
        project_id=_project().id,
        category=(body.get("category") or "general").strip(),
        rule_text=text,
    )
    return {"ok": True}


@route("/builder/rules/{rule_id:int}", methods=("DELETE",), capability="editor")
def drop_rule(request: Request):
    from db.database import deactivate_project_rule

    project = _project()
    if not deactivate_project_rule(request.path_params["rule_id"], project_id=project.id):
        raise NotFound("No such rule in this project.")
    return {"ok": True}
