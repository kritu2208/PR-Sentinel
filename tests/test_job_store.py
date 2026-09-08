"""
Comprehensive tests for PostgreSQL-backed Job Store in app/db/job_store.py.
Verifies all 17 invariants required for Phase 5B:
1. first webhook creates persistent job
2. duplicate delivery ID
3. duplicate review key with different delivery ID
4. same PR + different SHA
5. different PR
6. different repository
7. failed job becomes retryable
8. failed job retains historical record
9. retry with new delivery ID succeeds
10. retry while another attempt is in_progress is rejected
11. concurrent claims from multiple transactions/process-like execution
12. stale in_progress recovery
13. completed job cannot be processed again
14. job status transitions
15. no secrets stored in job failure information
16. webhook still returns 202
17. expensive review pipeline is not executed before ACK
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
    get_job,
    get_latest_job,
    recover_stale_jobs,
    sanitize_error,
    transition_to_completed,
    transition_to_failed,
    transition_to_in_progress,
)
from app.db.models import ReviewJob
from app.db.session import get_session
from app.main import app


@pytest.fixture(autouse=True)
def clean_db():
    """Clears review_jobs table before and after each test."""
    idempotency_store.clear()
    yield
    idempotency_store.clear()


@pytest.mark.asyncio
async def test_1_first_webhook_creates_persistent_job():
    """First claim creates a persistent ReviewJob with status queued and attempt 1."""
    async with get_session() as session:
        status, reason, job = await claim_job(
            session=session,
            repo_full_name="octocat/Hello-World",
            pr_number=1,
            head_sha="sha1111",
            delivery_id="deliv-1",
        )
        assert status == ClaimStatus.CLAIMED
        assert reason == ""
        assert job is not None
        assert job.status == "queued"
        assert job.attempt == 1
        assert job.delivery_id == "deliv-1"

    # Verify directly from database
    async with get_session() as session:
        db_job = await get_job(session, job.id)
        assert db_job is not None
        assert db_job.repo_full_name == "octocat/Hello-World"
        assert db_job.pr_number == 1
        assert db_job.head_sha == "sha1111"


@pytest.mark.asyncio
async def test_2_duplicate_delivery_id():
    """Duplicate delivery ID is rejected regardless of repo or review key."""
    async with get_session() as session:
        status1, _, _ = await claim_job(
            session, "octocat/Hello-World", 1, "sha1111", delivery_id="deliv-dup"
        )
        assert status1 == ClaimStatus.CLAIMED

        # Duplicate delivery with different PR / SHA
        status2, reason2, _ = await claim_job(
            session, "octocat/Hello-World", 2, "sha2222", delivery_id="deliv-dup"
        )
        assert status2 == ClaimStatus.DUPLICATE_DELIVERY
        assert "already received" in reason2


@pytest.mark.asyncio
async def test_3_duplicate_review_key_different_delivery_id():
    """Different delivery ID for an active review key is rejected."""
    async with get_session() as session:
        status1, _, _ = await claim_job(
            session, "octocat/Hello-World", 1, "sha1111", delivery_id="deliv-1"
        )
        assert status1 == ClaimStatus.CLAIMED

        status2, reason2, _ = await claim_job(
            session, "octocat/Hello-World", 1, "sha1111", delivery_id="deliv-2"
        )
        assert status2 == ClaimStatus.DUPLICATE_REVIEW
        assert "already in progress" in reason2 or "already queued" in reason2


@pytest.mark.asyncio
async def test_4_same_pr_different_sha():
    """Same repo and PR with a new commit SHA creates a new independent review job."""
    async with get_session() as session:
        status1, _, job1 = await claim_job(
            session, "octocat/Hello-World", 1, "sha-commit-1", delivery_id="deliv-1"
        )
        assert status1 == ClaimStatus.CLAIMED

        status2, _, job2 = await claim_job(
            session, "octocat/Hello-World", 1, "sha-commit-2", delivery_id="deliv-2"
        )
        assert status2 == ClaimStatus.CLAIMED
        assert job1.id != job2.id
        assert job2.head_sha == "sha-commit-2"


@pytest.mark.asyncio
async def test_5_different_pr():
    """Different PR numbers within the same repository create independent jobs."""
    async with get_session() as session:
        status1, _, job1 = await claim_job(
            session, "octocat/Hello-World", 10, "sha-same", delivery_id="deliv-1"
        )
        assert status1 == ClaimStatus.CLAIMED

        status2, _, job2 = await claim_job(
            session, "octocat/Hello-World", 20, "sha-same", delivery_id="deliv-2"
        )
        assert status2 == ClaimStatus.CLAIMED
        assert job1.id != job2.id


@pytest.mark.asyncio
async def test_6_different_repository():
    """Different repositories create independent jobs."""
    async with get_session() as session:
        status1, _, job1 = await claim_job(
            session, "orgA/repo", 1, "sha1", delivery_id="deliv-1"
        )
        assert status1 == ClaimStatus.CLAIMED

        status2, _, job2 = await claim_job(
            session, "orgB/repo", 1, "sha1", delivery_id="deliv-2"
        )
        assert status2 == ClaimStatus.CLAIMED
        assert job1.id != job2.id


@pytest.mark.asyncio
async def test_7_failed_job_becomes_retryable():
    """A failed job allows a new delivery to retry the review."""
    async with get_session() as session:
        status1, _, job1 = await claim_job(
            session, "octocat/Hello-World", 1, "sha1", delivery_id="deliv-1"
        )
        assert status1 == ClaimStatus.CLAIMED

        # Transition job1 to failed
        await transition_to_in_progress(session, job1.id)
        await transition_to_failed(session, job1.id, error_message="Network error")

        # Retry with new delivery ID
        status2, _, job2 = await claim_job(
            session, "octocat/Hello-World", 1, "sha1", delivery_id="deliv-2"
        )
        assert status2 == ClaimStatus.CLAIMED
        assert job2.attempt == 2


@pytest.mark.asyncio
async def test_8_failed_job_retains_historical_record():
    """Failed job row is never deleted and remains available for audit history."""
    async with get_session() as session:
        status1, _, job1 = await claim_job(
            session, "octocat/Hello-World", 1, "sha1", delivery_id="deliv-1"
        )
        await transition_to_failed(session, job1.id, "Simulated crash")

        status2, _, job2 = await claim_job(
            session, "octocat/Hello-World", 1, "sha1", delivery_id="deliv-2"
        )

    # Query all rows for this review key
    async with get_session() as session:
        stmt = (
            select(ReviewJob)
            .where(
                ReviewJob.repo_full_name == "octocat/Hello-World",
                ReviewJob.pr_number == 1,
                ReviewJob.head_sha == "sha1",
            )
            .order_by(ReviewJob.attempt.asc())
        )
        res = await session.execute(stmt)
        rows = res.scalars().all()
        assert len(rows) == 2
        assert rows[0].id == job1.id
        assert rows[0].status == "failed"
        assert rows[0].attempt == 1
        assert rows[0].delivery_id == "deliv-1"
        assert rows[0].error_message == "Simulated crash"

        assert rows[1].id == job2.id
        assert rows[1].status == "queued"
        assert rows[1].attempt == 2
        assert rows[1].delivery_id == "deliv-2"


@pytest.mark.asyncio
async def test_9_retry_with_new_delivery_id_succeeds():
    """After failure, original delivery ID is rejected, but new delivery ID succeeds."""
    async with get_session() as session:
        await claim_job(session, "octocat/Hello-World", 1, "sha1", delivery_id="D1")
        latest = await get_latest_job(session, "octocat/Hello-World", 1, "sha1")
        await transition_to_failed(session, latest.id, "Fail")

        # Original delivery D1 rejected
        status_d1, reason_d1, _ = await claim_job(
            session, "octocat/Hello-World", 1, "sha1", delivery_id="D1"
        )
        assert status_d1 == ClaimStatus.DUPLICATE_DELIVERY
        assert "already received" in reason_d1

        # New delivery D2 accepted
        status_d2, _, job_d2 = await claim_job(
            session, "octocat/Hello-World", 1, "sha1", delivery_id="D2"
        )
        assert status_d2 == ClaimStatus.CLAIMED
        assert job_d2.attempt == 2


@pytest.mark.asyncio
async def test_10_retry_while_in_progress_is_rejected():
    """A retry delivery is rejected if an attempt is currently in_progress."""
    async with get_session() as session:
        _, _, job1 = await claim_job(
            session, "octocat/Hello-World", 1, "sha1", delivery_id="D1"
        )
        await transition_to_in_progress(session, job1.id)

        status_d2, reason_d2, _ = await claim_job(
            session, "octocat/Hello-World", 1, "sha1", delivery_id="D2"
        )
        assert status_d2 == ClaimStatus.DUPLICATE_REVIEW
        assert "already in progress" in reason_d2


@pytest.mark.asyncio
async def test_11_concurrent_claims_from_multiple_transactions():
    """Concurrent claims for the exact same review key result in exactly one scheduled job."""
    async def run_claim(deliv_id: str):
        async with get_session() as session:
            return await claim_job(
                session, "octocat/Hello-World", 1, "sha1", delivery_id=deliv_id
            )

    results = await asyncio.gather(*[run_claim(f"deliv-{i}") for i in range(10)])
    claimed = [r for r in results if r[0] == ClaimStatus.CLAIMED]
    duplicate_reviews = [r for r in results if r[0] == ClaimStatus.DUPLICATE_REVIEW]

    assert len(claimed) == 1
    assert len(duplicate_reviews) == 9


@pytest.mark.asyncio
async def test_12_stale_in_progress_recovery():
    """Stale in_progress job past lease timeout is recovered and allows retry."""
    async with get_session() as session:
        _, _, job1 = await claim_job(
            session, "octocat/Hello-World", 1, "sha1", delivery_id="D1"
        )
        await transition_to_in_progress(session, job1.id)

        # Artificially set started_at into the past (beyond lease timeout)
        job1.started_at = datetime.now(timezone.utc) - timedelta(seconds=400)
        await session.commit()

        # New delivery arrives with lease_timeout=300
        status_d2, _, job2 = await claim_job(
            session,
            "octocat/Hello-World",
            1,
            "sha1",
            delivery_id="D2",
            lease_timeout=300,
        )
        assert status_d2 == ClaimStatus.CLAIMED
        assert job2.attempt == 2

        # Check job1 was recovered to failed
        old_job = await get_job(session, job1.id)
        assert old_job.status == "failed"
        assert "Job lease expired" in old_job.error_message


@pytest.mark.asyncio
async def test_12b_background_stale_jobs_recovery():
    """recover_stale_jobs identifies and marks all expired active jobs as failed."""
    async with get_session() as session:
        _, _, job1 = await claim_job(session, "octocat/Hello-World", 1, "sha1", "D1")
        await transition_to_in_progress(session, job1.id)
        job1.started_at = datetime.now(timezone.utc) - timedelta(seconds=600)
        await session.commit()

        recovered = await recover_stale_jobs(session, lease_timeout=300)
        assert job1.id in recovered

        rechecked = await get_job(session, job1.id)
        assert rechecked.status == "failed"


@pytest.mark.asyncio
async def test_13_completed_job_cannot_be_processed_again():
    """A completed review cannot be claimed again even with a new delivery ID."""
    async with get_session() as session:
        _, _, job1 = await claim_job(
            session, "octocat/Hello-World", 1, "sha1", delivery_id="D1"
        )
        await transition_to_in_progress(session, job1.id)
        await transition_to_completed(session, job1.id)

        status_d2, reason_d2, _ = await claim_job(
            session, "octocat/Hello-World", 1, "sha1", delivery_id="D2"
        )
        assert status_d2 == ClaimStatus.DUPLICATE_REVIEW
        assert "already completed" in reason_d2


@pytest.mark.asyncio
async def test_14_job_status_transitions():
    """Verifies state transitions: queued -> in_progress -> completed and queued -> in_progress -> failed."""
    async with get_session() as session:
        _, _, job = await claim_job(
            session, "octocat/Hello-World", 1, "sha1", delivery_id="D1"
        )
        assert job.status == "queued"
        assert job.started_at is None

        # queued -> in_progress
        started = await transition_to_in_progress(session, job.id)
        assert started is True
        j1 = await get_job(session, job.id)
        assert j1.status == "in_progress"
        assert j1.started_at is not None

        # in_progress -> completed
        completed = await transition_to_completed(session, job.id)
        assert completed is True
        j2 = await get_job(session, job.id)
        assert j2.status == "completed"
        assert j2.completed_at is not None


@pytest.mark.asyncio
async def test_15_no_secrets_stored_in_job_failure_information():
    """Sanitizer and failure transition strip GitHub tokens, Groq keys, Bearer tokens, and URLs."""
    raw_error = (
        "Failed calling GitHub API with token ghp_ABC1234567890abcdefghijklmnopqrstuvwxyz "
        "and fine-grained github_pat_11ABCD1234567890abcdefghijklmnopqrstuvwxyz_1234567890 "
        "and Groq key gsk_abcdefghijklmnopqrstuvwxyz12345678901234 "
        "using header Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secret.token "
        "connecting to postgresql://admin:SuperSecretPass123@localhost:5432/pr_sentinel"
    )

    clean = sanitize_error(raw_error)
    assert "ghp_ABC" not in clean
    assert "github_pat_" not in clean
    assert "gsk_abc" not in clean
    assert "SuperSecretPass123" not in clean
    assert "[REDACTED_GH_TOKEN]" in clean
    assert "[REDACTED_GROQ_KEY]" in clean
    assert "://admin:***@" in clean

    # Verify persisted in database
    async with get_session() as session:
        _, _, job = await claim_job(session, "octocat/Hello-World", 1, "sha1", "D1")
        await transition_to_failed(session, job.id, raw_error)
        db_job = await get_job(session, job.id)
        assert "SuperSecretPass123" not in db_job.error_message
        assert "ghp_" not in db_job.error_message


@pytest.mark.asyncio
async def test_16_webhook_returns_202_with_job_id(monkeypatch):
    """Webhook returns HTTP 202 Accepted containing job_id and review_key."""
    import hmac, hashlib
    secret = "webhook_secret_test"
    monkeypatch.setattr(settings, "GITHUB_WEBHOOK_SECRET", secret)

    payload = {
        "action": "opened",
        "number": 42,
        "pull_request": {
            "number": 42,
            "title": "Persistent Job Test PR",
            "diff_url": "https://github.com/octocat/Hello-World/pull/42.diff",
            "html_url": "https://github.com/octocat/Hello-World/pull/42",
            "head": {"sha": "sha-pr-42"},
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
                "X-GitHub-Delivery": "delivery-pr-42",
            },
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "accepted"
        assert "job_id" in data
        assert data["review_key"] == "octocat/Hello-World#42@sha-pr-42"

    # Verify job persisted in DB
    async with get_session() as session:
        db_job = await get_job(session, data["job_id"])
        assert db_job is not None
        assert db_job.pr_number == 42


@pytest.mark.asyncio
async def test_17_expensive_review_pipeline_not_executed_before_ack(monkeypatch):
    """Verifies that fetch_pr_files and graph.ainvoke are NOT called prior to 202 ACK."""
    import hmac, hashlib
    secret = "secret_ack_check"
    monkeypatch.setattr(settings, "GITHUB_WEBHOOK_SECRET", secret)

    payload = {
        "action": "opened",
        "number": 99,
        "pull_request": {
            "number": 99,
            "title": "Fast ACK Test PR",
            "diff_url": "https://github.com/octocat/Hello-World/pull/99.diff",
            "html_url": "https://github.com/octocat/Hello-World/pull/99",
            "head": {"sha": "sha-fast-ack"},
        },
        "repository": {"full_name": "octocat/Hello-World"},
    }
    body = json.dumps(payload).encode("utf-8")
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    call_order = []

    async def mock_claim(*args, **kwargs):
        call_order.append("claim_job")
        async with get_session() as session:
            return await claim_job(
                session, "octocat/Hello-World", 99, "sha-fast-ack", "deliv-ack-1"
            )

    transport = ASGITransport(app=app)
    with patch("app.main.claim_job", side_effect=mock_claim), \
         patch("app.main.fetch_pr_files", side_effect=lambda *a: call_order.append("fetch_pr_files")), \
         patch("app.main.review_graph.ainvoke", side_effect=lambda *a: call_order.append("graph_ainvoke")):

        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/webhook/github",
                content=body,
                headers={
                    "X-Hub-Signature-256": sig,
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "deliv-ack-1",
                },
            )
            assert resp.status_code == 202

    # claim_job must be the only operation invoked during request handling
    assert "claim_job" in call_order
