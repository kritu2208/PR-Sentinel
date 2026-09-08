"""
Unit tests for database-backed IdempotencyStore in app/agent/idempotency.py.
Verifies atomic claims, delivery deduplication, review deduplication,
lease timeout recovery, and retry transitions against PostgreSQL.
"""
import asyncio
import pytest
from app.agent.idempotency import IdempotencyStore, idempotency_store


@pytest.fixture(autouse=True)
def clean_db():
    """Ensures each test starts with a clean database."""
    idempotency_store.clear()
    yield
    idempotency_store.clear()


@pytest.mark.asyncio
async def test_idempotency_claim_delivery_and_review_key():
    """First claim succeeds; duplicate delivery ID or review key is rejected."""
    store = IdempotencyStore()

    # First claim
    claimed, reason = await store.claim(delivery_id="deliv-1", review_key="repo#1@sha1")
    assert claimed is True
    assert reason == ""

    # Duplicate delivery ID with different review key
    claimed, reason = await store.claim(delivery_id="deliv-1", review_key="repo#1@sha2")
    assert claimed is False
    assert "already received" in reason

    # Different delivery ID with duplicate review key
    claimed, reason = await store.claim(delivery_id="deliv-2", review_key="repo#1@sha1")
    assert claimed is False
    assert "already in progress" in reason


@pytest.mark.asyncio
async def test_idempotency_completed_state():
    """Completed review key remains protected against duplicate processing."""
    store = IdempotencyStore()

    await store.claim(delivery_id="deliv-1", review_key="repo#1@sha1")
    await store.mark_completed("repo#1@sha1")

    assert await store.get_status("repo#1@sha1") == "completed"

    # Subsequent delivery for completed review is rejected
    claimed, reason = await store.claim(delivery_id="deliv-2", review_key="repo#1@sha1")
    assert claimed is False
    assert "already completed" in reason


@pytest.mark.asyncio
async def test_idempotency_failed_state_allows_retry():
    """Failed review key is recoverable and allows subsequent claims to retry."""
    store = IdempotencyStore()

    await store.claim(delivery_id="deliv-1", review_key="repo#1@sha1")
    await store.mark_failed("repo#1@sha1")

    assert await store.get_status("repo#1@sha1") == "failed"

    # Subsequent delivery with new delivery_id is allowed to re-claim
    claimed, reason = await store.claim(delivery_id="deliv-2", review_key="repo#1@sha1")
    assert claimed is True
    assert reason == ""
    assert await store.get_status("repo#1@sha1") == "in_progress"


@pytest.mark.asyncio
async def test_idempotency_in_progress_timeout():
    """Abandoned or stuck in-progress reviews expire after in_progress_timeout and allow re-claim."""
    store = IdempotencyStore(in_progress_timeout=0.05)

    await store.claim(delivery_id="deliv-1", review_key="repo#1@sha1")
    await asyncio.sleep(0.06)

    # After timeout, new delivery can claim
    claimed, reason = await store.claim(delivery_id="deliv-2", review_key="repo#1@sha1")
    assert claimed is True
    assert reason == ""


@pytest.mark.asyncio
async def test_idempotency_concurrent_claims_atomic():
    """Concurrent claims for the exact same key result in exactly one winner."""
    store = IdempotencyStore()

    async def try_claim(d_id: str):
        return await store.claim(delivery_id=d_id, review_key="repo#1@sha1")

    # 10 concurrent requests
    results = await asyncio.gather(*[try_claim(f"deliv-{i}") for i in range(10)])
    successful_claims = [claimed for claimed, _ in results if claimed]

    assert len(successful_claims) == 1


@pytest.mark.asyncio
async def test_idempotency_different_sha_and_pr():
    """Same repo + PR with new commit SHA is allowed; different PR is allowed."""
    store = IdempotencyStore()

    # Claim PR 1 SHA 1
    claimed, _ = await store.claim("deliv-1", "repo#1@sha1")
    assert claimed is True

    # Claim PR 1 SHA 2 (new commit)
    claimed2, _ = await store.claim("deliv-2", "repo#1@sha2")
    assert claimed2 is True

    # Claim PR 2 SHA 1 (different PR)
    claimed3, _ = await store.claim("deliv-3", "repo#2@sha1")
    assert claimed3 is True


@pytest.mark.asyncio
async def test_idempotency_different_repo():
    """Different repositories are independent jobs."""
    store = IdempotencyStore()

    claimed1, _ = await store.claim("deliv-1", "orgA/repo#1@sha1")
    assert claimed1 is True

    claimed2, _ = await store.claim("deliv-2", "orgB/repo#1@sha1")
    assert claimed2 is True


@pytest.mark.asyncio
async def test_idempotency_failure_and_delivery_retry_flow():
    """
    Verifies full lifecycle:
    D1 + K1 claimed -> review fails -> D1 again ignored -> D2 + K1 succeeds as retry.
    """
    store = IdempotencyStore()

    # Step 1: D1 + K1 claimed
    claimed1, _ = await store.claim("D1", "repo#1@sha1")
    assert claimed1 is True

    # Step 2: D1 fails
    await store.mark_failed("repo#1@sha1")
    assert await store.get_status("repo#1@sha1") == "failed"

    # Step 3: D1 resubmitted -> rejected because delivery ID D1 was already seen
    claimed_d1_again, reason_d1 = await store.claim("D1", "repo#1@sha1")
    assert claimed_d1_again is False
    assert "already received" in reason_d1

    # Step 4: D2 (new delivery) + K1 -> accepted as retry
    claimed_d2, reason_d2 = await store.claim("D2", "repo#1@sha1")
    assert claimed_d2 is True
    assert reason_d2 == ""
    assert await store.get_status("repo#1@sha1") == "in_progress"
