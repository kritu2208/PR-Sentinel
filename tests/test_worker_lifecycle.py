"""
Tests for Phase 6 Worker Lifecycle, Graceful Drainage, and Transient Error Retries:
- In-flight job drainage on worker stop()
- Cancelled job lease release back to queued
- Pipeline transient error triggers worker backoff retry
- GitHub commit status check client integration
"""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
import uuid
import httpx
import pytest

from app.agent.state import ReviewState
from app.config import settings
from app.db.job_store import (
    claim_job,
    get_job,
    reset_in_progress_job_to_queued,
    transition_to_in_progress,
)
from app.db.session import get_session
from app.github_client import create_commit_status
from app.worker import Worker


@pytest.mark.asyncio
async def test_worker_cancelled_job_resets_to_queued():
    """When a worker execution task is cancelled, the job is reset to queued with cleared lease."""
    uid = uuid.uuid4().hex[:8]
    job_id = None
    worker_id = f"cancel-worker-{uid}"

    async with get_session() as session:
        _, _, job = await claim_job(
            session, "octocat/Hello-World", 201, f"sha_cancel_{uid}", delivery_id=f"deliv-cancel-{uid}"
        )
        job_id = job.id
        await transition_to_in_progress(session, job_id, worker_id=worker_id, lease_timeout=300)

    # Invoke reset_in_progress_job_to_queued
    async with get_session() as session:
        ok = await reset_in_progress_job_to_queued(session, job_id, worker_id)
        assert ok is True

    # Verify state in DB
    async with get_session() as session:
        j = await get_job(session, job_id)
        assert j.status == "queued"
        assert j.worker_id is None
        assert j.lease_expires_at is None
        assert j.started_at is None


@pytest.mark.asyncio
async def test_worker_transient_error_schedules_backoff_retry():
    """When the review graph returns transient_error, the worker schedules an exponential retry."""
    uid = uuid.uuid4().hex[:8]
    job_id = None
    worker_id = f"trans-worker-{uid}"

    async with get_session() as session:
        _, _, job = await claim_job(
            session, "octocat/Hello-World", 202, f"sha_trans_{uid}", delivery_id=f"deliv-trans-{uid}"
        )
        job_id = job.id

    w = Worker(worker_id=worker_id, base_delay=3.0, max_attempts=3)

    # Mock execute_job to simulate transient error detection from analyzer/poster
    mock_result = {
        "final_verdict": "comment",
        "aggregated_findings": [],
        "error": "transient_error",
    }

    with patch.object(w, "execute_job", return_value=mock_result):
        await w.process_job_by_id(job_id)

    async with get_session() as session:
        old_job = await get_job(session, job_id)
        assert old_job.status == "failed"
        assert "transient error" in old_job.error_message.lower()

        # Check that retry job was created
        from sqlalchemy import select
        from app.db.models import ReviewJob
        stmt = (
            select(ReviewJob)
            .where(
                ReviewJob.repo_full_name == "octocat/Hello-World",
                ReviewJob.head_sha == f"sha_trans_{uid}",
                ReviewJob.attempt == 2,
            )
        )
        res = await session.execute(stmt)
        retry_job = res.scalars().first()
        assert retry_job is not None
        assert retry_job.status == "queued"
        assert retry_job.next_retry_at is not None


@pytest.mark.asyncio
async def test_worker_graceful_shutdown_waits_for_inflight_job():
    """Worker stop() pauses to allow an in-flight job up to WORKER_SHUTDOWN_TIMEOUT to finish."""
    w = Worker(worker_id="shutdown-test-worker")
    w._running = True
    w._current_job_id = 9999

    finished_job = False

    async def simulate_in_flight():
        nonlocal finished_job
        await asyncio.sleep(0.3)
        w._current_job_id = None
        finished_job = True

    sim_task = asyncio.create_task(simulate_in_flight())

    with patch.object(settings, "WORKER_SHUTDOWN_TIMEOUT", 2.0):
        await w.stop()

    await sim_task
    assert finished_job is True
    assert w._running is False
    assert w._current_job_id is None


@pytest.mark.asyncio
async def test_github_create_commit_status_client():
    """create_commit_status sends correct JSON payload to GitHub Statuses API."""
    req = httpx.Request("POST", "https://api.github.com/repos/owner/repo/statuses/sha123")
    resp = httpx.Response(status_code=201, json={"id": 12345, "state": "success"}, request=req)

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=resp) as mock_post:
        res = await create_commit_status(
            repo_full_name="owner/repo",
            commit_sha="sha123",
            state="success",
            description="PR Sentinel: Clean review",
            context="pr-sentinel/review",
        )
        assert res["state"] == "success"
        mock_post.assert_awaited_once()
        kall = mock_post.await_args
        assert "/repos/owner/repo/statuses/sha123" in kall[0][0]
        assert kall.kwargs["json"]["state"] == "success"
        assert kall.kwargs["json"]["context"] == "pr-sentinel/review"
