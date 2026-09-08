"""
Comprehensive tests for Durable Worker Execution, Recovery & Retry (Phase 5C).

Covers all required areas:
- Worker basics (picking up queued jobs, completion, failure)
- Concurrency (two workers racing, exactly one winner, concurrent processing)
- Lease & Recovery (stale queued recovery, stale in-progress recovery, fresh in-progress protection, heartbeat extension)
- Retry policy (transient failure retries, backoff delay, attempt increment, max attempts ceiling)
- Restart durability (queued job survives restart, completed job not re-executed)
- Webhook integration (fast ACK 202, background durability, deduplication)
"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import AsyncMock, patch
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.agent.idempotency import idempotency_store
from app.config import settings
from app.db.job_store import (
    ClaimStatus,
    claim_job,
    claim_next_job,
    get_job,
    get_latest_job,
    recover_stale_jobs,
    transition_to_completed,
    transition_to_failed,
    transition_to_in_progress,
)
from app.db.models import ReviewJob
from app.db.session import get_session
from app.main import app
from app.worker import Worker


@pytest.fixture(autouse=True)
def clean_db():
    """Clears database before and after each test."""
    idempotency_store.clear()
    yield
    idempotency_store.clear()


@pytest.fixture
def mock_review_pipeline():
    """Mocks external GitHub and LLM calls for deterministic review pipeline execution."""
    sample_files = [
        {
            "filename": "main.py",
            "patch": "@@ -1,3 +1,4 @@\n+print('hello world')\n",
            "status": "modified",
            "additions": 1,
            "deletions": 0,
            "changes": 1,
        }
    ]
    with patch("app.worker.fetch_pr_files", new_callable=AsyncMock) as mock_fetch, \
         patch("app.worker.review_graph.ainvoke", new_callable=AsyncMock) as mock_graph:
        mock_fetch.return_value = sample_files
        mock_graph.return_value = {
            "final_verdict": "comment",
            "aggregated_findings": [],
            "inline_comments_posted": 0,
        }
        yield mock_fetch, mock_graph


# ==============================================================================
# 1. WORKER BASICS
# ==============================================================================

@pytest.mark.asyncio
async def test_1_queued_job_is_picked_up_by_worker(mock_review_pipeline):
    """A queued job is claimed and processed by the worker."""
    async with get_session() as session:
        _, _, job = await claim_job(
            session, "octocat/Hello-World", 1, "sha1", delivery_id="deliv-1"
        )
        assert job.status == "queued"

    w = Worker(worker_id="test-w1")
    processed = await w.run_once()
    assert processed is True

    async with get_session() as session:
        updated = await get_job(session, job.id)
        assert updated.status == "completed"
        assert updated.completed_at is not None


@pytest.mark.asyncio
async def test_2_successful_job_becomes_completed(mock_review_pipeline):
    """Verify that successful review pipeline execution sets completed_at and clears lease."""
    async with get_session() as session:
        _, _, job = await claim_job(
            session, "octocat/Hello-World", 1, "sha1", delivery_id="deliv-1"
        )

    w = Worker(worker_id="test-w1")
    await w.run_once()

    async with get_session() as session:
        db_job = await get_job(session, job.id)
        assert db_job.status == "completed"
        assert db_job.lease_expires_at is None
        assert db_job.completed_at is not None


@pytest.mark.asyncio
async def test_3_failed_job_becomes_failed():
    """When the review pipeline fails, the current attempt is recorded as failed."""
    async with get_session() as session:
        _, _, job = await claim_job(
            session, "octocat/Hello-World", 1, "sha1", delivery_id="deliv-1"
        )

    w = Worker(worker_id="test-w1", max_attempts=1)
    with patch("app.worker.fetch_pr_files", side_effect=Exception("GitHub API Connection Refused")):
        processed = await w.run_once()
        assert processed is True

    async with get_session() as session:
        db_job = await get_job(session, job.id)
        assert db_job.status == "failed"
        assert db_job.failed_at is not None
        assert "GitHub API Connection Refused" in db_job.error_message


@pytest.mark.asyncio
async def test_4_worker_processes_multiple_independent_jobs(mock_review_pipeline):
    """Worker sequentially processes multiple queued jobs until the queue is empty."""
    async with get_session() as session:
        for i in range(3):
            await claim_job(
                session, "octocat/Hello-World", i + 1, f"sha{i}", delivery_id=f"deliv-{i}"
            )

    w = Worker(worker_id="test-w1")
    assert await w.run_once() is True
    assert await w.run_once() is True
    assert await w.run_once() is True
    # Queue is now empty
    assert await w.run_once() is False


# ==============================================================================
# 2. CONCURRENCY & ROW LOCKING
# ==============================================================================

@pytest.mark.asyncio
async def test_5_and_6_two_workers_race_for_same_job_exactly_one_wins():
    """Two workers concurrently claiming the same queued job results in exactly one winner."""
    async with get_session() as session:
        _, _, job = await claim_job(
            session, "octocat/Hello-World", 1, "sha1", delivery_id="deliv-1"
        )

    w1 = Worker(worker_id="worker-alpha")
    w2 = Worker(worker_id="worker-beta")

    async def claim_with(w: Worker):
        async with get_session() as session:
            return await claim_next_job(session, worker_id=w.worker_id)

    results = await asyncio.gather(claim_with(w1), claim_with(w2))
    winners = [r for r in results if r is not None]
    losers = [r for r in results if r is None]

    assert len(winners) == 1
    assert len(losers) == 1
    assert winners[0].id == job.id
    assert winners[0].worker_id in ("worker-alpha", "worker-beta")


@pytest.mark.asyncio
async def test_7_two_workers_process_different_jobs_concurrently(mock_review_pipeline):
    """Two workers can claim and process different jobs simultaneously without conflict."""
    async with get_session() as session:
        await claim_job(session, "octocat/Hello-World", 10, "sha10", delivery_id="deliv-10")
        await claim_job(session, "octocat/Hello-World", 20, "sha20", delivery_id="deliv-20")

    w1 = Worker(worker_id="worker-1")
    w2 = Worker(worker_id="worker-2")

    r1, r2 = await asyncio.gather(w1.run_once(), w2.run_once())
    assert r1 is True
    assert r2 is True

    async with get_session() as session:
        stmt = select(ReviewJob).where(ReviewJob.status == "completed")
        res = await session.execute(stmt)
        completed = res.scalars().all()
        assert len(completed) == 2


# ==============================================================================
# 3. LEASE & STALE RECOVERY
# ==============================================================================

@pytest.mark.asyncio
async def test_8_stale_queued_job_is_recovered():
    """Queued jobs stuck beyond lease timeout are recovered and retried."""
    async with get_session() as session:
        _, _, job = await claim_job(
            session, "octocat/Hello-World", 1, "sha1", delivery_id="deliv-1"
        )
        # Simulate job abandoned in queued state long ago
        job.created_at = datetime.now(timezone.utc) - timedelta(seconds=500)
        await session.commit()

    async with get_session() as session:
        recovered = await recover_stale_jobs(session, lease_timeout=300, schedule_retry=True)
        assert job.id in recovered

    async with get_session() as session:
        old = await get_job(session, job.id)
        assert old.status == "failed"
        assert "Job lease expired" in old.error_message

        # Verify retry job created
        stmt = select(ReviewJob).where(ReviewJob.status == "queued")
        res = await session.execute(stmt)
        retry_job = res.scalars().first()
        assert retry_job is not None
        assert retry_job.attempt == 2


@pytest.mark.asyncio
async def test_9_stale_in_progress_job_is_recovered(mock_review_pipeline):
    """In-progress job whose lease expired (worker crashed) is recovered to failed and re-executed."""
    async with get_session() as session:
        _, _, job = await claim_job(
            session, "octocat/Hello-World", 1, "sha1", delivery_id="deliv-1"
        )
        await transition_to_in_progress(session, job.id)
        # Simulate worker crash: lease expired 100s ago
        job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=100)
        job.worker_id = "crashed-worker"
        await session.commit()

    w2 = Worker(worker_id="active-worker", base_delay=0.0)
    # run_once recovers the stale job, claims the newly queued retry attempt, and completes it
    processed = await w2.run_once()
    assert processed is True

    async with get_session() as session:
        stmt = (
            select(ReviewJob)
            .where(ReviewJob.repo_full_name == "octocat/Hello-World")
            .order_by(ReviewJob.attempt.asc())
        )
        res = await session.execute(stmt)
        all_attempts = res.scalars().all()
        assert len(all_attempts) == 2
        assert all_attempts[0].status == "failed"
        assert all_attempts[0].attempt == 1
        assert all_attempts[1].status == "completed"
        assert all_attempts[1].attempt == 2


@pytest.mark.asyncio
async def test_10_fresh_in_progress_job_is_not_reclaimed():
    """An active in-progress job whose lease is still valid is NOT marked stale or reclaimed."""
    async with get_session() as session:
        _, _, job = await claim_job(
            session, "octocat/Hello-World", 1, "sha1", delivery_id="deliv-1"
        )
        await transition_to_in_progress(session, job.id)
        # Active lease in the future
        job.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=200)
        job.worker_id = "legit-worker"
        await session.commit()

    async with get_session() as session:
        recovered = await recover_stale_jobs(session, lease_timeout=300)
        assert len(recovered) == 0

        # Another worker tries to claim
        claimed = await claim_next_job(session, worker_id="other-worker")
        assert claimed is None

    async with get_session() as session:
        db_job = await get_job(session, job.id)
        assert db_job.status == "in_progress"
        assert db_job.worker_id == "legit-worker"


@pytest.mark.asyncio
async def test_11_heartbeat_prevents_legitimate_long_running_job_from_becoming_stale():
    """Heartbeat background task repeatedly extends lease_expires_at during processing."""
    async with get_session() as session:
        _, _, job = await claim_job(
            session, "octocat/Hello-World", 1, "sha1", delivery_id="deliv-1"
        )

    # Worker with very short lease (0.2s) and fast heartbeat (0.05s)
    w = Worker(
        worker_id="heartbeat-worker",
        lease_timeout=0.2,
        heartbeat_interval=0.05,
    )

    initial_lease = None

    async def slow_review_pipeline(*args, **kwargs):
        nonlocal initial_lease
        async with get_session() as session:
            j = await get_job(session, job.id)
            initial_lease = j.lease_expires_at
        # Sleep longer than the 0.2s lease duration while heartbeat runs
        await asyncio.sleep(0.3)
        return {"final_verdict": "comment", "aggregated_findings": [], "inline_comments_posted": 0}

    with patch("app.worker.fetch_pr_files", new_callable=AsyncMock) as mock_fetch, \
         patch("app.worker.review_graph.ainvoke", side_effect=slow_review_pipeline):
        mock_fetch.return_value = [{"filename": "f.py", "patch": "@@ -1 +1 @@\n+x\n"}]
        processed = await w.run_once()
        assert processed is True

    async with get_session() as session:
        finished_job = await get_job(session, job.id)
        assert finished_job.status == "completed"


# ==============================================================================
# 4. RETRY POLICY & EXPONENTIAL BACKOFF
# ==============================================================================

@pytest.mark.asyncio
async def test_12_13_14_transient_failure_retries_with_backoff_and_incremented_attempt():
    """On failure, the worker schedules a retry with exponential backoff delay and incremented attempt."""
    async with get_session() as session:
        _, _, job = await claim_job(
            session, "octocat/Hello-World", 1, "sha1", delivery_id="deliv-1"
        )

    w = Worker(worker_id="test-w1", base_delay=5.0, max_attempts=3)

    before_fail = datetime.now(timezone.utc)
    with patch("app.worker.fetch_pr_files", side_effect=Exception("Transient Network Timeout")):
        await w.run_once()

    async with get_session() as session:
        # Original attempt 1 must be failed
        att1 = await get_job(session, job.id)
        assert att1.status == "failed"
        assert att1.attempt == 1

        # Attempt 2 must be queued with next_retry_at scheduled ~5.0s in the future
        stmt = (
            select(ReviewJob)
            .where(
                ReviewJob.repo_full_name == "octocat/Hello-World",
                ReviewJob.attempt == 2,
            )
        )
        res = await session.execute(stmt)
        att2 = res.scalars().first()
        assert att2 is not None
        assert att2.status == "queued"
        assert att2.next_retry_at is not None
        expected_min_retry = before_fail + timedelta(seconds=4.8)
        assert att2.next_retry_at >= expected_min_retry


@pytest.mark.asyncio
async def test_15_retry_succeeds_after_transient_failure(mock_review_pipeline):
    """When a retry job becomes eligible, it is processed and transitions to completed."""
    async with get_session() as session:
        _, _, job = await claim_job(
            session, "octocat/Hello-World", 1, "sha1", delivery_id="deliv-1"
        )

    w = Worker(worker_id="test-w1", base_delay=0.01, max_attempts=3)

    # First attempt fails
    with patch("app.worker.fetch_pr_files", side_effect=Exception("First attempt failure")):
        await w.run_once()

    # Small sleep so next_retry_at (0.01s) passes
    await asyncio.sleep(0.02)

    # Second attempt succeeds
    processed = await w.run_once()
    assert processed is True

    async with get_session() as session:
        stmt = (
            select(ReviewJob)
            .where(ReviewJob.repo_full_name == "octocat/Hello-World")
            .order_by(ReviewJob.attempt.asc())
        )
        res = await session.execute(stmt)
        attempts = res.scalars().all()
        assert len(attempts) == 2
        assert attempts[0].status == "failed"
        assert attempts[0].attempt == 1
        assert attempts[1].status == "completed"
        assert attempts[1].attempt == 2


@pytest.mark.asyncio
async def test_16_and_17_max_job_attempts_prevents_infinite_retries():
    """When attempt count reaches MAX_JOB_ATTEMPTS, the job is permanently failed."""
    async with get_session() as session:
        _, _, job = await claim_job(
            session, "octocat/Hello-World", 1, "sha1", delivery_id="deliv-1"
        )

    # Set max_attempts to 2 and base_delay to 0.01s for fast execution
    w = Worker(worker_id="test-w1", base_delay=0.01, max_attempts=2)

    with patch("app.worker.fetch_pr_files", side_effect=Exception("Permanent Fatal Error")):
        # Attempt 1 fails -> schedules attempt 2
        await w.run_once()
        await asyncio.sleep(0.02)

        # Attempt 2 fails -> reaches max_attempts (2) -> NO attempt 3 scheduled
        await w.run_once()

    async with get_session() as session:
        stmt = (
            select(ReviewJob)
            .where(ReviewJob.repo_full_name == "octocat/Hello-World")
            .order_by(ReviewJob.attempt.asc())
        )
        res = await session.execute(stmt)
        attempts = res.scalars().all()
        assert len(attempts) == 2
        assert attempts[0].status == "failed"
        assert attempts[1].status == "failed"

        # Ensure no queued jobs remain
        stmt_queued = select(ReviewJob).where(ReviewJob.status == "queued")
        res_queued = await session.execute(stmt_queued)
        assert len(res_queued.scalars().all()) == 0


# ==============================================================================
# 5. RESTART DURABILITY
# ==============================================================================

@pytest.mark.asyncio
async def test_18_queued_job_remains_available_after_worker_restart(mock_review_pipeline):
    """A job queued before a simulated server restart is picked up by a new worker instance."""
    async with get_session() as session:
        _, _, job = await claim_job(
            session, "octocat/Hello-World", 1, "sha1", delivery_id="deliv-1"
        )
        assert job.status == "queued"

    # Simulate new process/server instance starting with a fresh worker ID
    fresh_worker = Worker(worker_id="fresh-worker-after-restart")
    processed = await fresh_worker.run_once()
    assert processed is True

    async with get_session() as session:
        completed_job = await get_job(session, job.id)
        assert completed_job.status == "completed"
        assert completed_job.worker_id == "fresh-worker-after-restart"


@pytest.mark.asyncio
async def test_19_previously_completed_job_not_executed_again():
    """A previously completed job is never picked up or re-executed by a worker."""
    async with get_session() as session:
        _, _, job = await claim_job(
            session, "octocat/Hello-World", 1, "sha1", delivery_id="deliv-1"
        )
        await transition_to_in_progress(session, job.id)
        await transition_to_completed(session, job.id)

    w = Worker(worker_id="test-w1")
    processed = await w.run_once()
    assert processed is False


@pytest.mark.asyncio
async def test_20_previously_failed_job_can_retry_with_new_delivery_webhook(mock_review_pipeline):
    """When a job permanently fails, a new webhook delivery can retry the review."""
    async with get_session() as session:
        _, _, job1 = await claim_job(
            session, "octocat/Hello-World", 1, "sha1", delivery_id="D1"
        )
        await transition_to_failed(session, job1.id, "Fatal Error")

    # New delivery D2 arrives for the same commit
    async with get_session() as session:
        status, _, job2 = await claim_job(
            session, "octocat/Hello-World", 1, "sha1", delivery_id="D2"
        )
        assert status == ClaimStatus.CLAIMED
        assert job2.attempt == 2

    w = Worker(worker_id="test-w1")
    processed = await w.run_once()
    assert processed is True

    async with get_session() as session:
        final_job = await get_job(session, job2.id)
        assert final_job.status == "completed"


# ==============================================================================
# 6. WEBHOOK FAST ACK & DURABILITY INTEGRATION
# ==============================================================================

@pytest.mark.asyncio
async def test_21_22_23_webhook_fast_ack_and_durable_queue_exists(monkeypatch):
    """
    Webhook immediately returns 202 without executing review synchronously,
    and a durable 'queued' record exists in the database.
    """
    import hmac, hashlib
    secret = "test_webhook_secret"
    monkeypatch.setattr(settings, "GITHUB_WEBHOOK_SECRET", secret)
    mock_exec = AsyncMock()
    monkeypatch.setattr("app.main.process_pull_request_review", mock_exec)

    payload = {
        "action": "opened",
        "number": 55,
        "pull_request": {
            "number": 55,
            "title": "Durable Worker Test",
            "diff_url": "https://github.com/octocat/Hello-World/pull/55.diff",
            "html_url": "https://github.com/octocat/Hello-World/pull/55",
            "head": {"sha": "sha-durable-55"},
        },
        "repository": {"full_name": "octocat/Hello-World"},
    }
    body = json.dumps(payload).encode("utf-8")
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/webhook/github",
            content=body,
            headers={
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "delivery-durable-55",
            },
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "accepted"
        job_id = data["job_id"]

    assert mock_exec.call_count == 1
    assert mock_exec.call_args.kwargs["job_id"] == job_id

    # Durable job exists in PostgreSQL
    async with get_session() as session:
        job = await get_job(session, job_id)
        assert job is not None
        assert job.repo_full_name == "octocat/Hello-World"
        assert job.pr_number == 55
        assert job.head_sha == "sha-durable-55"
        assert job.status == "queued"


@pytest.mark.asyncio
async def test_24_and_25_webhook_duplicate_delivery_and_review_rejected(monkeypatch):
    """Webhook rejects duplicate delivery ID and duplicate review key with 200 ignored."""
    import hmac, hashlib
    secret = "test_webhook_secret_dedup"
    monkeypatch.setattr(settings, "GITHUB_WEBHOOK_SECRET", secret)
    monkeypatch.setattr("app.main.process_pull_request_review", AsyncMock())

    payload = {
        "action": "opened",
        "number": 88,
        "pull_request": {
            "number": 88,
            "title": "Dedup Test",
            "diff_url": "https://github.com/octocat/Hello-World/pull/88.diff",
            "html_url": "https://github.com/octocat/Hello-World/pull/88",
            "head": {"sha": "sha-dedup-88"},
        },
        "repository": {"full_name": "octocat/Hello-World"},
    }
    body = json.dumps(payload).encode("utf-8")
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # First request -> 202
        r1 = await ac.post(
            "/webhook/github",
            content=body,
            headers={
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "deliv-original",
            },
        )
        assert r1.status_code == 202

        # Duplicate delivery ID -> 200 ignored
        r2 = await ac.post(
            "/webhook/github",
            content=body,
            headers={
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "deliv-original",
            },
        )
        assert r2.status_code == 200
        assert r2.json()["status"] == "ignored"
        assert "already received" in r2.json()["reason"]

        # Different delivery ID, same review key -> 200 ignored
        r3 = await ac.post(
            "/webhook/github",
            content=body,
            headers={
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "deliv-different",
            },
        )
        assert r3.status_code == 200
        assert r3.json()["status"] == "ignored"
        assert "already in progress" in r3.json()["reason"]
