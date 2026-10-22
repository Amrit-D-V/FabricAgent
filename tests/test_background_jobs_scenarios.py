from __future__ import annotations

import time
from core.jobs import (
    create_job,
    update_job_progress,
    complete_job,
    fail_job,
    get_job,
    submit_background_job,
)


def test_job_lifecycle():
    job_id = create_job("test_task", project_id=123)
    assert job_id is not None

    job = get_job(job_id)
    assert job is not None
    assert job["id"] == job_id
    assert job["status"] == "queued"
    assert job["project_id"] == 123

    update_job_progress(job_id, "Running step 1", status="running")
    job = get_job(job_id)
    assert job["status"] == "running"
    assert job["progress_msg"] == "Running step 1"

    complete_job(job_id, {"result_count": 42})
    job = get_job(job_id)
    assert job["status"] == "completed"
    assert job["result"] == {"result_count": 42}


def test_job_failure():
    job_id = create_job("failing_task")
    fail_job(job_id, "Something went wrong")

    job = get_job(job_id)
    assert job["status"] == "failed"
    assert job["error_msg"] == "Something went wrong"


def test_submit_background_job_execution():
    def dummy_worker(on_progress):
        on_progress("halfway")
        return {"done": True}

    job_id = submit_background_job("dummy_job", dummy_worker, project_id=99)
    assert job_id is not None

    # Wait briefly for thread to finish
    for _ in range(20):
        job = get_job(job_id)
        if job and job["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)

    assert job["status"] == "completed"
    assert job["result"] == {"done": True}
