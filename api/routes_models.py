"""Semantic models: listing, DAX generation, and building one from a file."""

from __future__ import annotations

import io
import os

from starlette.requests import Request

from api.registry import json_body, route
from auth.auth import visible_projects
from core.context import state
from core.errors import AccessDenied, AppError, NotFound


def _project():
    # api.registry has already required one; this resolves it. Both go through
    # auth.auth.require_project so there is a single definition of "selected".
    from auth.auth import require_project

    return require_project()


def _models(project_id: int):
    from db.database import list_semantic_models

    return [m for m in list_semantic_models(project_id) if m.tmdl_files]


def _model(project_id: int, model_id: int):
    model = next((m for m in _models(project_id) if m.id == model_id), None)
    if not model:
        raise NotFound("No saved model with stored TMDL for that id.")
    return model


@route("/models")
def list_models(_request: Request):
    from core.reports import model_stats

    return [
        {
            "id": m.id,
            "name": m.model_name,
            "createdAt": m.created_at,
            "stats": model_stats(m.tmdl_files),
        }
        for m in _models(_project().id)
    ]


@route("/models/{model_id:int}/history")
def model_history(request: Request):
    """Retrieve historical snapshots and versions of this model for diff and rollback."""
    project_id = _project().id
    model_id = request.path_params["model_id"]
    model = _model(project_id, model_id)
    from db.database import list_semantic_models

    all_versions = [
        {
            "id": m.id,
            "name": m.model_name,
            "createdAt": m.created_at,
            "fileCount": len(m.tmdl_files or {}),
            "isActive": m.id == model_id,
        }
        for m in list_semantic_models(project_id)
        if m.model_name == model.model_name
    ]
    return {"versions": all_versions}


@route("/models/{model_id:int}/diff", methods=("POST",), capability="editor")
async def model_diff(request: Request):
    """Compute visual schema diff between two model versions."""
    body = await json_body(request)
    target_id = int(body.get("targetModelId") or 0)
    project_id = _project().id
    current_model = _model(project_id, request.path_params["model_id"])
    target_model = _model(project_id, target_id)

    curr_files = set(current_model.tmdl_files.keys())
    target_files = set(target_model.tmdl_files.keys())

    added = sorted(list(target_files - curr_files))
    removed = sorted(list(curr_files - target_files))
    common = curr_files & target_files

    modified = [
        f for f in common
        if current_model.tmdl_files.get(f) != target_model.tmdl_files.get(f)
    ]

    import difflib
    diff_details: dict[str, list[str]] = {}
    for f in modified:
        curr_lines = (current_model.tmdl_files.get(f) or "").splitlines()
        target_lines = (target_model.tmdl_files.get(f) or "").splitlines()
        diff = list(
            difflib.unified_diff(
                curr_lines,
                target_lines,
                fromfile=f"Current/{f}",
                tofile=f"Target/{f}",
                lineterm="",
            )
        )
        diff_details[f] = diff

    return {
        "currentId": current_model.id,
        "targetId": target_model.id,
        "addedFiles": added,
        "removedFiles": removed,
        "modifiedFiles": modified,
        "diffDetails": diff_details,
    }



@route("/models/{model_id:int}/rollback", methods=("POST",), capability="editor")
async def model_rollback(request: Request):
    """Roll back model definition to a prior historical version."""
    body = await json_body(request)
    target_id = int(body.get("targetModelId") or 0)
    project_id = _project().id
    target_model = _model(project_id, target_id)

    from db.database import save_semantic_model
    restored = save_semantic_model(
        project_id=project_id,
        model_name=target_model.model_name,
        tmdl_files=target_model.tmdl_files,
        wireframe_id=target_model.wireframe_id,
        server_fqdn=target_model.server_fqdn,
        db_name=target_model.db_name,
    )
    return {
        "ok": True,
        "restoredId": restored.id,
        "modelName": restored.model_name,
    }



@route("/models/dax", methods=("POST",), capability="editor")
async def dax(request: Request):
    """A DAX query grounded in one model's real measures."""
    from dax.dax_assistant import generate_dax

    body = await json_body(request)
    question = (body.get("question") or "").strip()
    if not question:
        raise AppError("Ask a question first.")
    model = _model(_project().id, int(body.get("modelId") or 0))
    try:
        return {"dax": generate_dax(question, model.tmdl_files)}
    except Exception as exc:
        raise AppError(f"Generation failed: {exc}") from exc


@route("/models/dax/execute", methods=("POST",), capability="editor")
async def execute_dax_playground(request: Request):
    """Execute raw DAX queries against connected Fabric XMLA or analyze offline."""
    body = await json_body(request)
    query = (body.get("query") or "").strip()
    if not query:
        raise AppError("Provide a DAX query to execute.")

    server = (body.get("server") or "").strip()
    database = (body.get("database") or "").strip()

    import model

    # Offline static analysis & smells
    smells = model.dax_smells(query)
    explanation = model.explain_dax(query, {}, {})

    # Live execution if server & database are provided
    table_result = ""
    error_msg = ""
    if server and database:
        from fabric.pbi_xmla import run_dax
        table_result = run_dax(server, database, query)
        if table_result.startswith("DAX error:"):
            error_msg = table_result
            table_result = ""

    return {
        "query": query,
        "result": table_result,
        "error": error_msg,
        "smells": [{"issue": s[0], "fix": s[1]} for s in smells],
        "explanation": explanation,
    }


@route("/models/dax/fix", methods=("POST",), capability="editor")
async def fix_dax_query(request: Request):
    """Auto-fix anti-patterns or syntax slips in a proposed DAX query."""
    body = await json_body(request)
    query = (body.get("query") or "").strip()
    if not query:
        raise AppError("Provide a DAX query to fix.")

    import model
    from dax.dax_assistant import rewrite_dax_fix

    error_ctx = (body.get("error") or "").strip()
    optimized = model.optimize_dax(query)
    smells = model.dax_smells(query)
    fixed = rewrite_dax_fix(query, error_ctx)

    return {
        "original": query,
        "fixed": fixed,
        "optimizationNotes": optimized,
        "smells": [{"issue": s[0], "fix": s[1]} for s in smells],
    }




@route("/models/from-file", methods=("POST",), capability="editor")
async def from_file(request: Request):
    """Build a semantic model straight from a CSV or Excel upload.

    No Fabric warehouse involved — the file itself becomes the source, which
    is the whole point of this path.
    """
    import pandas as pd

    from db.database import save_document, save_semantic_model
    from model.csv_model import dataframe_to_tmdl, infer_columns

    project = _project()
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "filename"):
        raise AppError("Attach a CSV or Excel file.")

    raw = await upload.read()
    is_excel = upload.filename.lower().endswith((".xlsx", ".xls"))
    try:
        frame = (
            pd.read_excel(io.BytesIO(raw))
            if is_excel
            else pd.read_csv(io.BytesIO(raw))
        )
    except Exception as exc:
        raise AppError(f"Could not read the file: {exc}") from exc

    if frame.empty:
        raise AppError("The file has no rows.")

    # Infer date columns if >= 90% of values parse as datetime
    for col in frame.columns:
        if frame[col].dtype.kind == "O":
            parsed = pd.to_datetime(frame[col], errors="coerce")
            if isinstance(parsed, pd.Series) and float(parsed.notna().mean()) > 0.9:
                frame[col] = parsed

    stem = os.path.splitext(upload.filename)[0]
    table_name = (str(form.get("tableName") or "") or stem[:50] or "Data").strip()
    model_name = (
        str(form.get("modelName") or "") or f"{stem[:40]}_model".lstrip("_") or "file_model"
    ).strip()

    schema = infer_columns(frame)
    doc = save_document(
        project.id,
        upload.filename,
        raw,
        file_type=getattr(upload, "content_type", "")
        or ("excel" if is_excel else "csv"),
        description="data source for quick model",
    )
    files = dataframe_to_tmdl(
        frame, table_name, model_name, source_path=doc.file_path, is_excel=is_excel
    )
    saved = save_semantic_model(
        project_id=project.id,
        model_name=model_name,
        tmdl_files=files,
        wireframe_id=None,
        server_fqdn="",
        db_name="",
    )
    return {
        "id": saved.id,
        "name": model_name,
        "rows": int(len(frame)),
        "schema": [{"column": c, "type": t} for c, t, *_ in schema],
        "files": sorted(files),
    }


@route("/models/preview-file", methods=("POST",), capability="editor")
async def preview_file(request: Request):
    """Rows and inferred schema for an upload, before committing to a model."""
    import pandas as pd

    from model.csv_model import infer_columns

    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "filename"):
        raise AppError("Attach a CSV or Excel file.")

    raw = await upload.read()
    is_excel = upload.filename.lower().endswith((".xlsx", ".xls"))
    try:
        frame = (
            pd.read_excel(io.BytesIO(raw))
            if is_excel
            else pd.read_csv(io.BytesIO(raw))
        )
    except Exception as exc:
        raise AppError(f"Could not read the file: {exc}") from exc
    if frame.empty:
        raise AppError("The file has no rows.")

    head = frame.head(20)
    stem = os.path.splitext(upload.filename)[0]
    return {
        "rows": int(len(frame)),
        "columns": [str(c) for c in frame.columns],
        "head": [
            [None if v != v else str(v) for v in row]  # NaN is not JSON
            for row in head.itertuples(index=False, name=None)
        ],
        "schema": [{"column": c, "type": t} for c, t, *_ in infer_columns(frame)],
        "suggestedTable": stem[:50] or "Data",
        "suggestedModel": f"{stem[:40]}_model".lstrip("_") or "file_model",
    }
