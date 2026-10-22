"""Coverage, validation and export for a project's saved semantic models.

Everything here is capped at "editor" by the user's own Power BI access to the
project's workspace -- a Fabric Viewer cannot write through FabricBI's service
principal, which is what require_capability enforces.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response

from api.registry import json_body, route
from auth.auth import visible_projects
from core import reports
from core.errors import AccessDenied, NotFound

_MIME = {
    "md": ("text/markdown", "dictionary.md"),
    "xlsx": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "dictionary.xlsx",
    ),
    "pptx": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "report.pptx",
    ),
    "pdf": ("application/pdf", "report.pdf"),
}


def _project(request: Request):
    from auth.auth import require_project

    return require_project()


def _models(project):
    from db.database import list_semantic_models

    return [m for m in list_semantic_models(project.id) if m.tmdl_files]


def _model(project, model_id: int):
    model = next((m for m in _models(project) if m.id == model_id), None)
    if not model:
        raise NotFound("No saved model with stored TMDL for that id.")
    return model


@route("/reports/sources", capability="editor")
def sources(_request: Request):
    """The wireframes and models the pickers offer."""
    from db.database import list_wireframes

    project = _project(_request)
    return {
        "project": {"id": project.id, "name": project.name},
        "wireframes": [
            {
                "id": w.id,
                "title": w.title,
                "status": w.status,
                "requested": reports.requested_items(w.spec),
            }
            for w in list_wireframes(project.id)
        ],
        "models": [
            {
                "id": m.id,
                "name": m.model_name,
                "createdAt": m.created_at,
                "stats": reports.model_stats(m.tmdl_files),
            }
            for m in _models(project)
        ],
    }


@route("/reports/coverage", methods=("POST",), capability="editor")
async def coverage(request: Request):
    body = await json_body(request)
    project = _project(request)
    model = _model(project, int(body.get("modelId") or 0))
    requested = [str(x).strip() for x in (body.get("requested") or []) if str(x).strip()]
    if not requested:
        raise NotFound("Add at least one requested item to compute coverage.")
    return reports.coverage(requested, model.tmdl_files, model.model_name)


@route("/reports/export", capability="editor")
def export(request: Request):
    """Stream one artifact. `format` is md | xlsx | pptx | pdf."""
    from db.database import list_wireframes

    project = _project(request)
    fmt = request.query_params.get("format", "md")
    if fmt not in _MIME:
        raise NotFound(f"Unknown format {fmt!r}.")
    model = _model(project, int(request.query_params.get("modelId") or 0))

    built = reports.build_artifacts(
        model.id,
        model.model_name,
        model.tmdl_files,
        reports.approved_coverage(model.tmdl_files, list_wireframes(project.id)),
    )
    payload = built["markdown"].encode("utf-8") if fmt == "md" else built[fmt]
    media_type, suffix = _MIME[fmt]
    return Response(
        payload,
        media_type=media_type,
        headers={
            "content-disposition": (
                f'attachment; filename="{model.model_name}_{suffix}"'
            )
        },
    )


@route("/reports/dictionary", capability="editor")
def dictionary(request: Request):
    """The data dictionary as markdown, for previewing in the page."""
    from db.database import list_wireframes

    project = _project(request)
    model = _model(project, int(request.query_params.get("modelId") or 0))
    built = reports.build_artifacts(
        model.id,
        model.model_name,
        model.tmdl_files,
        reports.approved_coverage(model.tmdl_files, list_wireframes(project.id)),
    )
    return {"markdown": built["markdown"]}


# --- validation --------------------------------------------------------------


@route("/reports/validate/scan", methods=("POST",), capability="editor")
async def scan(request: Request):
    """Parse a report's visuals into the bindings that can be checked.

    The dataset id comes from the same folder, so the caller does not have to
    know how a .Report directory is laid out.
    """
    from model.validation_core import extract_dataset_id, parse_report_visuals

    body = await json_body(request)
    path = (body.get("reportPath") or "").strip()
    if not path:
        raise NotFound("Which report should be scanned?")
    try:
        visuals = parse_report_visuals(path)
        dataset_id = extract_dataset_id(path)
    except OSError as exc:
        raise NotFound(f"Cannot read that report: {exc}") from exc
    return {"datasetId": dataset_id, "visuals": [_as_json(v) for v in visuals or []]}


@route("/reports/validate/run", methods=("POST",), capability="editor")
async def run_validation(request: Request):
    """Execute a visual's DAX against the model and its SQL against the
    warehouse, then compare the two answers."""
    from model.validation_core import compare_results, execute_dax, execute_sql

    body = await json_body(request)
    dax = (body.get("dax") or "").strip()
    sql = (body.get("sql") or "").strip()
    if not dax and not sql:
        raise NotFound("Nothing to run — provide DAX, SQL, or both.")

    dax_result = (
        execute_dax(
            body.get("datasetId") or "",
            body.get("workspaceId") or "",
            dax,
        )
        if dax
        else None
    )
    sql_result = (
        execute_sql(
            body.get("serverFqdn") or "",
            body.get("database") or "",
            sql,
        )
        if sql
        else None
    )

    comparison = (
        compare_results(dax_result, sql_result)
        if dax_result is not None and sql_result is not None
        else None
    )

    return {
        "dax": _as_json(dax_result),
        "sql": _as_json(sql_result),
        "comparison": _as_json(comparison),
    }


def _as_json(value):
    """validation_core speaks in dataclasses; the wire speaks in JSON."""
    import dataclasses

    if value is None:
        return None
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, dict):
        return value
    return {"value": str(value)}
