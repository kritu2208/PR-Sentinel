"""
Tests for GitHub Rate-Limit Awareness and Dynamic Retry Scheduling.
Covers parse_rate_limit_headers for Retry-After, X-RateLimit-Reset, and fail_and_schedule_retry integration.
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
import httpx
import pytest

from app.db.job_store import claim_job, fail_and_schedule_retry, get_job
from app.db.session import get_session
from app.github_client import parse_rate_limit_headers
from app.worker import Worker


def test_parse_rate_limit_headers_429_retry_after():
    """HTTP 429 with standard Retry-After header extracts seconds correctly."""
    req = httpx.Request("POST", "https://api.github.com")
    resp = httpx.Response(status_code=429, headers={"Retry-After": "45"}, request=req)
    delay = parse_rate_limit_headers(resp)
    assert delay == 45.0


def test_parse_rate_limit_headers_403_primary_rate_limit_reset():
    """HTTP 403 with x-ratelimit-remaining: 0 and x-ratelimit-reset computes delay relative to now."""
    req = httpx.Request("POST", "https://api.github.com")
    now_ts = 1700000000.0
    reset_ts = 1700000120.0  # 120 seconds in future
    headers = {
        "x-ratelimit-remaining": "0",
        "x-ratelimit-reset": str(reset_ts),
    }
    resp = httpx.Response(status_code=403, headers=headers, request=req)
    delay = parse_rate_limit_headers(resp, now_ts=now_ts)
    assert delay == 120.0


def test_parse_rate_limit_headers_non_rate_limit_status():
    """Non-403/429 status codes (e.g. 200, 404, 500) return None."""
    req = httpx.Request("GET", "https://api.github.com")
    resp_200 = httpx.Response(status_code=200, request=req)
    assert parse_rate_limit_headers(resp_200) is None

    resp_404 = httpx.Response(status_code=404, request=req)
    assert parse_rate_limit_headers(resp_404) is None

    resp_500 = httpx.Response(status_code=500, request=req)
    assert parse_rate_limit_headers(resp_500) is None


def test_parse_rate_limit_headers_403_without_zero_remaining():
    """HTTP 403 without x-ratelimit-remaining=0 (e.g. forbidden permission error) returns None."""
    req = httpx.Request("GET", "https://api.github.com")
    resp_403 = httpx.Response(
        status_code=403,
        headers={"x-ratelimit-remaining": "100", "x-ratelimit-reset": "1700000120"},
        request=req,
    )
    assert parse_rate_limit_headers(resp_403) is None


@pytest.mark.asyncio
async def test_fail_and_schedule_retry_with_exact_delay():
    """fail_and_schedule_retry respects exact_delay parameter when passed."""
    import uuid
    uid = uuid.uuid4().hex[:8]
    async with get_session() as session:
        _, _, job = await claim_job(
            session, "octocat/Hello-World", 101, f"sha_rate_{uid}", delivery_id=f"deliv-rate-{uid}"
        )
        before_time = datetime.now(timezone.utc)
        ok, retry_job = await fail_and_schedule_retry(
            session=session,
            job_id=job.id,
            error_message="Rate limited",
            base_delay=2.0,
            max_attempts=3,
            exact_delay=60.0,
        )

        assert ok is True
        assert retry_job is not None
        assert retry_job.attempt == 2
        assert retry_job.next_retry_at is not None
        delta = (retry_job.next_retry_at - before_time).total_seconds()
        # Should be scheduled approximately 60 seconds in future, not 2.0s
        assert 58.0 <= delta <= 65.0


@pytest.mark.asyncio
async def test_worker_catches_rate_limit_and_schedules_exact_delay():
    """Worker catches HTTP 429 and schedules next_retry_at using Retry-After header."""
    import uuid
    uid = uuid.uuid4().hex[:8]
    async with get_session() as session:
        _, _, job = await claim_job(
            session, "octocat/Hello-World", 102, f"sha_rate_w_{uid}", delivery_id=f"deliv-rate-w-{uid}"
        )

    w = Worker(worker_id="rate-test-worker", base_delay=2.0)
    req = httpx.Request("POST", "https://api.github.com")
    resp_429 = httpx.Response(status_code=429, headers={"Retry-After": "75"}, request=req)
    err = httpx.HTTPStatusError("Too Many Requests", request=req, response=resp_429)

    with patch("app.worker.fetch_pr_files", side_effect=err):
        await w.process_job_by_id(job.id)

    async with get_session() as session:
        old_job = await get_job(session, job.id)
        assert old_job.status == "failed"
        assert "rate limited; retry in 75.0s" in old_job.error_message

        from sqlalchemy import select
        from app.db.models import ReviewJob
        stmt = (
            select(ReviewJob)
            .where(
                ReviewJob.repo_full_name == "octocat/Hello-World",
                ReviewJob.head_sha == f"sha_rate_w_{uid}",
                ReviewJob.attempt == 2,
            )
        )
        res = await session.execute(stmt)
        retry_job = res.scalars().first()
        assert retry_job.attempt == 2
        assert retry_job.status == "queued"
        # Verify delay is approximately 75 seconds
        delta = (retry_job.next_retry_at - old_job.failed_at).total_seconds()
        assert 73.0 <= delta <= 77.0


def test_parse_rate_limit_headers_429_http_date_retry_after():
    """HTTP 429 with RFC 7231 HTTP-date Retry-After header extracts seconds correctly."""
    req = httpx.Request("POST", "https://api.github.com")
    # Date: Wed, 21 Oct 2026 07:28:00 GMT
    # 1792567680 is epoch timestamp for 2026-10-21 07:28:00 UTC
    resp = httpx.Response(
        status_code=429,
        headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"},
        request=req,
    )
    now_ts = 1792567600.0  # 80 seconds earlier
    delay = parse_rate_limit_headers(resp, now_ts=now_ts)
    assert delay == 80.0

