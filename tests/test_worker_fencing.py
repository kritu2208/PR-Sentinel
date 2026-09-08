"""
Tests for Worker Side-Effect Fencing and Lease Guards.
Verifies that a partitioned or stale worker whose lease expired or was reassigned
aborts before publishing reviews or finalizing completions.
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
import pytest

from app.agent.nodes.poster import poster_node
from app.db.job_store import claim_job, get_job, transition_to_in_progress
from app.db.session import get_session
from app.worker import Worker


@pytest.mark.asyncio
async def test_poster_fencing_aborts_when_worker_id_mismatched():
    """Poster node aborts review publication if another worker now holds the lease."""
    import uuid
    uid = uuid.uuid4().hex[:8]
    async with get_session() as session:
        _, _, job = await claim_job(
            session, "octocat/Hello-World", 201, f"sha_fence_1_{uid}", delivery_id=f"deliv-fence-1-{uid}"
        )
        # Job was claimed by worker-A
        await transition_to_in_progress(session, job.id, worker_id="worker-A", lease_timeout=300)

    # State belongs to partitioned worker-B
    state = {
        "job_id": job.id,
        "worker_id": "worker-B",  # Does NOT match worker-A
        "repo_full_name": "octocat/Hello-World",
        "pr_number": 201,
        "commit_sha": f"sha_fence_1_{uid}",
        "changed_files": [],
        "aggregated_findings": [],
        "final_summary": "Stale worker summary",
        "final_verdict": "approve",
    }

    with patch("app.agent.nodes.poster.create_pull_request_review", new_callable=AsyncMock) as mock_review:
        result = await poster_node(state)

        # External GitHub API must NEVER be called
        mock_review.assert_not_called()
        assert result.get("error") == "fencing_violation"
        assert result.get("github_review_id") is None


@pytest.mark.asyncio
async def test_poster_fencing_aborts_when_job_status_not_in_progress():
    """Poster node aborts review publication if job was already completed or failed."""
    import uuid
    uid = uuid.uuid4().hex[:8]
    async with get_session() as session:
        _, _, job = await claim_job(
            session, "octocat/Hello-World", 202, f"sha_fence_2_{uid}", delivery_id=f"deliv-fence-2-{uid}"
        )
        # Job marked failed
        job.status = "failed"
        await session.commit()

    state = {
        "job_id": job.id,
        "worker_id": "worker-A",
        "repo_full_name": "octocat/Hello-World",
        "pr_number": 202,
        "commit_sha": f"sha_fence_2_{uid}",
        "changed_files": [],
        "aggregated_findings": [],
        "final_summary": "Summary",
        "final_verdict": "comment",
    }

    with patch("app.agent.nodes.poster.create_pull_request_review", new_callable=AsyncMock) as mock_review:
        result = await poster_node(state)

        mock_review.assert_not_called()
        assert result.get("error") == "fencing_violation"


@pytest.mark.asyncio
async def test_worker_execute_job_fencing_check():
    """Worker.execute_job verifies active lease ownership and returns None if fenced."""
    import uuid
    uid = uuid.uuid4().hex[:8]
    async with get_session() as session:
        _, _, job = await claim_job(
            session, "octocat/Hello-World", 203, f"sha_fence_3_{uid}", delivery_id=f"deliv-fence-3-{uid}"
        )
        # Assigned to worker-legit
        await transition_to_in_progress(session, job.id, worker_id="worker-legit", lease_timeout=300)

    # Worker with different ID tries to execute it
    stale_worker = Worker(worker_id="worker-stale")
    with patch("app.worker.fetch_pr_files", new_callable=AsyncMock) as mock_fetch:
        result = await stale_worker.execute_job(job.id)

        assert result is None
        mock_fetch.assert_not_called()

    # Verify job in DB was untouched
    async with get_session() as session:
        db_job = await get_job(session, job.id)
        assert db_job.status == "in_progress"
        assert db_job.worker_id == "worker-legit"
