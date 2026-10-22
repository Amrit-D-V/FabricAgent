"""Project documents that business requirements are extracted from."""

from __future__ import annotations

import os

from starlette.requests import Request

from api.registry import route
from core.errors import AppError, NotFound

_EXT_TO_TYPE = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".json": "application/json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def _project():
    from auth.auth import require_project

    return require_project()


@route("/documents", capability="editor")
def list_docs(_request: Request):
    from db.database import list_documents

    return [
        {
            "id": d.id,
            "name": d.original_name or d.filename,
            "fileType": d.file_type,
            "size": d.file_size,
            "uploadedAt": d.uploaded_at,
        }
        for d in list_documents(_project().id)
    ]


@route("/documents", methods=("POST",), capability="editor")
async def upload_doc(request: Request):
    """Attach a document file to the project."""
    from db.database import save_document

    project = _project()
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "filename"):
        raise AppError("Attach a file to upload.")

    raw = await upload.read()
    suffix = os.path.splitext(upload.filename)[1].lower()
    declared = getattr(upload, "content_type", "") or ""
    saved = save_document(
        project_id=project.id,
        original_name=upload.filename,
        file_bytes=raw,
        file_type=_EXT_TO_TYPE.get(suffix, declared or "application/octet-stream"),
    )
    return {"id": saved.id, "name": saved.original_name}


@route("/documents/{doc_id:int}", methods=("DELETE",), capability="editor")
def delete_doc(request: Request):
    """Delete an attached document."""
    from db.database import delete_document, get_document

    project = _project()
    doc = get_document(request.path_params["doc_id"])
    if not doc or doc.project_id != project.id:
        raise NotFound("No such document in this project.")
    delete_document(doc.id)
    return {"ok": True}
