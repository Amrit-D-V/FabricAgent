from __future__ import annotations

from starlette.requests import Request

from api.registry import json_body, route
from core.errors import NotFound
from core.jobs import get_job


@route("/jobs/{job_id:str}")
def job_status(_request: Request, job_id: str):
    job = get_job(job_id)
    if not job:
        raise NotFound(f"Job {job_id!r} not found.")
    return {
        "id": job["id"],
        "projectId": job.get("project_id"),
        "jobType": job["job_type"],
        "status": job["status"],
        "progressMessage": job.get("progress_msg") or "",
        "result": job.get("result"),
        "error": job.get("error_msg"),
        "createdAt": job.get("created_at"),
        "updatedAt": job.get("updated_at"),
    }
