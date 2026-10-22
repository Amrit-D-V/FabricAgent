from __future__ import annotations

from starlette.requests import Request
from starlette.responses import FileResponse, Response

from api.registry import route
from core import storage
from core.context import current_email
from core.errors import AppError, NotFound


@route("/storage/roots", capability="admin", project=False)
def roots(_request: Request):
    return storage.overview()


@route("/storage/list", capability="admin", project=False)
def list_dir(request: Request):
    q = request.query_params
    result = storage.list_dir(q.get("root", ""), q.get("path", ""))
    if "error" in result:
        raise NotFound(result["error"])
    return result


@route("/storage/preview", capability="admin", project=False)
def preview(request: Request):
    q = request.query_params
    result = storage.preview(q.get("root", ""), q.get("path", ""))
    if result.get("kind") == "error":
        raise NotFound(result["message"])
    return result


@route("/storage/file", capability="admin", project=False)
def download(request: Request):
    """Stream one file. FileResponse streams from disk rather than reading it
    all into memory, which is what capped the Streamlit version at 100 MB."""
    from db.database import audit

    q = request.query_params
    found = storage.resolve(q.get("root", ""), q.get("path", ""))
    if not found or not found[1].is_file():
        raise NotFound("That file is not available.")
    target = found[1]

    size = target.stat().st_size
    audit(
        "storage.download",
        target=str(target),
        details=storage.fmt_size(size),
        user_email=current_email(),
    )
    inline = q.get("inline") == "1"
    return FileResponse(
        target,
        filename=target.name,
        content_disposition_type="inline" if inline else "attachment",
    )


@route("/storage/zip", capability="admin", project=False)
def zip_dir(request: Request):
    from db.database import audit

    q = request.query_params
    found = storage.resolve(q.get("root", ""), q.get("path", ""))
    if not found or not found[1].is_dir():
        raise NotFound("That folder is not available.")
    root, target = found

    blob, added, truncated = storage.zip_tree(target, target.name or root.name)
    if not added:
        raise AppError("Nothing to zip in that folder.")
    audit(
        "storage.zip",
        target=str(target),
        details=f"{added} files{' (truncated)' if truncated else ''}",
        user_email=current_email(),
    )
    return Response(
        blob,
        media_type="application/zip",
        headers={
            "content-disposition": f'attachment; filename="{target.name or "storage"}.zip"',
            "x-fabricbi-files": str(added),
            "x-fabricbi-truncated": "1" if truncated else "0",
        },
    )
