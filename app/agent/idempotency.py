"""
PostgreSQL-backed idempotency store and job adapter for PR Sentinel.
Delegates all claim, deduplication, and status operations to the persistent
database job store in app.db.job_store.
"""
from datetime import datetime, timezone
from typing import Literal
import logging
import sqlite3
import psycopg

from app.config import settings
from app.db.job_store import (
    ClaimStatus,
    claim_job,
    get_latest_job,
    transition_to_completed,
    transition_to_failed,
)
from app.db.session import get_session

logger = logging.getLogger("pr-sentinel.idempotency")

ReviewStatus = Literal["queued", "in_progress", "completed", "failed"]


def _parse_review_key(review_key: str) -> tuple[str, int, str]:
    """Parses 'repo#pr@sha' into (repo_full_name, pr_number, head_sha)."""
    try:
        repo_and_rest = review_key.split("#")
        repo = repo_and_rest[0]
        pr_and_sha = repo_and_rest[1].split("@")
        pr = int(pr_and_sha[0])
        sha = pr_and_sha[1]
        return repo, pr, sha
    except Exception:
        # Fallback if unformatted
        return review_key, 0, "unknown"


class IdempotencyStore:
    """
    Durable, database-backed idempotency store.
    Ensures that delivery ID deduplication, review state, and active processing
    are persisted in PostgreSQL and survive application restarts.
    """

    def __init__(self, in_progress_timeout: float | None = None) -> None:
        self.in_progress_timeout = in_progress_timeout

    async def claim(
        self,
        delivery_id: str | None,
        review_key: str,
    ) -> tuple[bool, str]:
        """
        Atomically checks and claims processing rights in the database.
        Returns (True, "") if successfully claimed.
        Returns (False, reason) if duplicate delivery or duplicate review.
        """
        repo, pr, sha = _parse_review_key(review_key)
        async with get_session() as session:
            claim_status, reason, _ = await claim_job(
                session=session,
                repo_full_name=repo,
                pr_number=pr,
                head_sha=sha,
                delivery_id=delivery_id,
                lease_timeout=self.in_progress_timeout,
                initial_status="in_progress",
            )
            return claim_status == ClaimStatus.CLAIMED, reason

    async def mark_completed(self, review_key: str) -> None:
        """Marks the latest job for review_key as completed."""
        repo, pr, sha = _parse_review_key(review_key)
        async with get_session() as session:
            job = await get_latest_job(session, repo, pr, sha)
            if job:
                await transition_to_completed(session, job.id)

    async def mark_failed(self, review_key: str, error_message: str | None = None) -> None:
        """Marks the latest job for review_key as failed so it can be retried."""
        repo, pr, sha = _parse_review_key(review_key)
        async with get_session() as session:
            job = await get_latest_job(session, repo, pr, sha)
            if job:
                await transition_to_failed(session, job.id, error_message)

    async def get_status(self, review_key: str) -> ReviewStatus | None:
        """Returns the current status of the latest job for review_key."""
        repo, pr, sha = _parse_review_key(review_key)
        async with get_session() as session:
            job = await get_latest_job(session, repo, pr, sha)
            if not job:
                return None
            now = datetime.now(timezone.utc)
            # If the latest job is queued with a future next_retry_at, the current observable state
            # is that the previous execution failed and backoff retry is pending.
            if job.status == "queued" and job.next_retry_at is not None and job.next_retry_at > now:
                return "failed"
            return job.status

    def clear(self) -> None:
        """Synchronously clears review_jobs from the database (for test isolation)."""
        url = settings.DATABASE_URL
        if "sqlite" in url:
            db_path = url.replace("sqlite+aiosqlite:///", "").replace("sqlite:///", "")
            if db_path and db_path != ":memory:":
                conn = sqlite3.connect(db_path)
                conn.execute("DELETE FROM review_jobs")
                conn.commit()
                conn.close()
        else:
            conn_url = url.replace("+asyncpg", "")
            try:
                conn = psycopg.connect(conn_url, autocommit=True)
                conn.execute("DELETE FROM review_jobs")
                conn.close()
            except Exception as exc:
                logger.warning("Could not clear review_jobs: %s", exc)


idempotency_store = IdempotencyStore()
