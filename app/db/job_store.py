"""
Database-backed job store and atomic claim operations for PR Sentinel.
"""
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
import logging
import re
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import ReviewJob

logger = logging.getLogger("pr-sentinel.job-store")


class ClaimStatus(str, Enum):
    CLAIMED = "claimed"
    DUPLICATE_DELIVERY = "duplicate_delivery"
    DUPLICATE_REVIEW = "duplicate_review"


def sanitize_error(error_msg: str | None) -> str | None:
    """
    Sanitizes failure messages to ensure no sensitive credentials, tokens,
    or secret keys are stored in the database or logs.
    """
    if not error_msg:
        return None

    sanitized = error_msg

    # 1. GitHub Tokens (classic ghp_ and fine-grained github_pat_)
    sanitized = re.sub(r"ghp_[A-Za-z0-9_]{36,}", "[REDACTED_GH_TOKEN]", sanitized)
    sanitized = re.sub(r"github_pat_[A-Za-z0-9_]{50,}", "[REDACTED_GH_PAT]", sanitized)

    # 2. Groq API keys
    sanitized = re.sub(r"gsk_[A-Za-z0-9_]{40,}", "[REDACTED_GROQ_KEY]", sanitized)

    # 3. HTTP Authorization / Bearer tokens
    sanitized = re.sub(
        r"(Bearer\s+)[A-Za-z0-9_\-\.]+", r"\1[REDACTED_TOKEN]", sanitized, flags=re.IGNORECASE
    )

    # 4. URLs with embedded credentials (e.g. postgresql://user:pass@host:port/db)
    sanitized = re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", sanitized)

    # 5. Generic credential patterns in query strings or headers
    sanitized = re.sub(
        r"(password|token|secret|api_key|apikey)=([^&\s]+)",
        r"\1=[REDACTED]",
        sanitized,
        flags=re.IGNORECASE,
    )

    # Cap length to prevent unbounded text storage
    if len(sanitized) > 1000:
        sanitized = sanitized[:997] + "..."

    return sanitized


def _ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def claim_job(
    session: AsyncSession,
    repo_full_name: str,
    pr_number: int,
    head_sha: str,
    delivery_id: str | None,
    lease_timeout: float | None = None,
    initial_status: str = "queued",
) -> tuple[ClaimStatus, str, ReviewJob | None]:
    """
    Atomically checks and claims processing rights for a delivery and review key.

    Invariants:
    1. Duplicate delivery_id returns DUPLICATE_DELIVERY.
    2. Active review (queued or in_progress within lease) returns DUPLICATE_REVIEW.
    3. Completed review returns DUPLICATE_REVIEW.
    4. Stale in_progress or queued review past lease_timeout is recovered to 'failed',
       allowing a new retry attempt to be claimed.
    5. Failed review allows a new delivery to retry with incremented attempt number.
    6. Database unique constraints provide final authority against race conditions.
    """
    review_key = f"{repo_full_name}#{pr_number}@{head_sha}"
    timeout = lease_timeout if lease_timeout is not None else settings.JOB_LEASE_TIMEOUT
    now = datetime.now(timezone.utc)

    try:
        # Step 1: Check delivery ID deduplication
        if delivery_id:
            deliv_stmt = select(ReviewJob).where(ReviewJob.delivery_id == delivery_id)
            deliv_res = await session.execute(deliv_stmt)
            existing_deliv = deliv_res.scalars().first()
            if existing_deliv:
                logger.info(
                    "Duplicate delivery: delivery_id=%s already recorded for %s (job_id=%s, status=%s)",
                    delivery_id,
                    review_key,
                    existing_deliv.id,
                    existing_deliv.status,
                )
                return (
                    ClaimStatus.DUPLICATE_DELIVERY,
                    f"Delivery {delivery_id} already received",
                    existing_deliv,
                )

        # Step 2: Query existing jobs for this review identity (repo, PR, commit SHA)
        query = (
            select(ReviewJob)
            .where(
                ReviewJob.repo_full_name == repo_full_name,
                ReviewJob.pr_number == pr_number,
                ReviewJob.head_sha == head_sha,
            )
            .order_by(ReviewJob.attempt.desc(), ReviewJob.id.desc())
        )

        # Lock rows in PostgreSQL to prevent concurrent claim race conditions
        bind = session.bind
        if bind is not None and bind.dialect.name != "sqlite":
            query = query.with_for_update()

        result = await session.execute(query)
        existing_job = result.scalars().first()

        next_attempt = 1
        if existing_job:
            current_status = existing_job.status

            if current_status == "completed":
                logger.info(
                    "Duplicate review: %s already completed (job_id=%s, attempt=%s)",
                    review_key,
                    existing_job.id,
                    existing_job.attempt,
                )
                return (
                    ClaimStatus.DUPLICATE_REVIEW,
                    f"Review for {review_key} has already completed",
                    existing_job,
                )

            elif current_status in ("queued", "in_progress"):
                # If the job is queued for a scheduled retry (next_retry_at is set),
                # a new webhook delivery immediately takes over that retry attempt
                if current_status == "queued" and existing_job.next_retry_at is not None:
                    next_attempt = existing_job.attempt
                    existing_job.status = "failed"
                    existing_job.next_retry_at = None
                    existing_job.error_message = "Superseded by incoming webhook delivery"
                    await session.flush()
                    logger.info(
                        "Scheduled retry superseded by new webhook delivery for %s (attempt %s)",
                        review_key,
                        next_attempt,
                    )
                else:
                    # Check for lease expiration (stale job recovery)
                    is_stale = False
                    if current_status == "in_progress" and existing_job.started_at:
                        elapsed = (now - _ensure_utc(existing_job.started_at)).total_seconds()
                        if elapsed > timeout:
                            is_stale = True
                    elif current_status == "queued" and existing_job.created_at:
                        elapsed = (now - _ensure_utc(existing_job.created_at)).total_seconds()
                        if elapsed > timeout:
                            is_stale = True

                    if is_stale:
                        # Recover stale job by marking it failed
                        existing_job.status = "failed"
                        existing_job.failed_at = now
                        existing_job.updated_at = now
                        existing_job.error_message = (
                            f"Job lease expired after {timeout}s (stale {current_status} recovery)"
                        )
                        await session.flush()
                        next_attempt = existing_job.attempt + 1
                        logger.warning(
                            "Stale job recovered: job_id=%s for %s timed out after %ss, marking failed and retrying",
                            existing_job.id,
                            review_key,
                            timeout,
                        )
                    else:
                        logger.info(
                            "Duplicate review: %s is currently %s (job_id=%s, attempt=%s)",
                            review_key,
                            current_status,
                            existing_job.id,
                            existing_job.attempt,
                        )
                        status_display = "in progress" if current_status in ("queued", "in_progress") else current_status
                        return (
                            ClaimStatus.DUPLICATE_REVIEW,
                            f"Review for {review_key} is already {status_display}",
                            existing_job,
                        )

            elif current_status == "failed":
                # Previous attempt failed; safe to retry with new attempt number
                next_attempt = existing_job.attempt + 1
                logger.info(
                    "Retrying failed review for %s (previous job_id=%s, new attempt=%s)",
                    review_key,
                    existing_job.id,
                    next_attempt,
                )

        # Step 3: Insert new persistent job row
        new_job = ReviewJob(
            delivery_id=delivery_id,
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            head_sha=head_sha,
            status=initial_status,
            attempt=next_attempt,
            created_at=now,
            updated_at=now,
            started_at=now if initial_status == "in_progress" else None,
        )
        session.add(new_job)
        await session.flush()
        await session.commit()

        logger.info(
            "Job created: job_id=%s, review_key=%s, attempt=%s, delivery_id=%s, status=queued",
            new_job.id,
            review_key,
            new_job.attempt,
            delivery_id,
        )
        return ClaimStatus.CLAIMED, "", new_job

    except IntegrityError as exc:
        await session.rollback()
        # Extract the database error message prior to the SQL text to avoid matching column names in INSERT
        db_err = str(exc).split("[SQL")[0].lower()

        if "uq_active_or_completed_review" in db_err:
            logger.info(
                "Concurrent duplicate review rejected by database constraint for %s",
                review_key,
            )
            return (
                ClaimStatus.DUPLICATE_REVIEW,
                f"Review for {review_key} is already in progress or completed",
                None,
            )

        if "delivery_id" in db_err:
            logger.info(
                "Concurrent duplicate delivery rejected by database constraint for delivery_id=%s",
                delivery_id,
            )
            return (
                ClaimStatus.DUPLICATE_DELIVERY,
                f"Delivery {delivery_id} already received",
                None,
            )

        logger.info(
            "Concurrent duplicate review rejected by database constraint for %s",
            review_key,
        )
        return (
            ClaimStatus.DUPLICATE_REVIEW,
            f"Review for {review_key} is already in progress or completed",
            None,
        )


async def transition_to_in_progress(
    session: AsyncSession,
    job_id: int,
    worker_id: str | None = None,
    lease_timeout: float | None = None,
) -> bool:
    """
    Atomically transitions a job from 'queued' to 'in_progress'.
    Optionally assigns worker_id and computes lease_expires_at.
    Returns True if transitioned, False if the job was not in 'queued' status.
    """
    now = datetime.now(timezone.utc)
    values = {
        "status": "in_progress",
        "started_at": now,
        "updated_at": now,
    }
    if worker_id is not None:
        values["worker_id"] = worker_id
    if lease_timeout is not None:
        values["lease_expires_at"] = now + timedelta(seconds=lease_timeout)

    stmt = (
        update(ReviewJob)
        .where(ReviewJob.id == job_id, ReviewJob.status == "queued")
        .values(**values)
    )
    result = await session.execute(stmt)
    await session.commit()

    if result.rowcount > 0:
        logger.info("Job started: job_id=%s transitioned to in_progress", job_id)
        return True

    logger.warning("Failed to start job: job_id=%s was not in queued status", job_id)
    return False


async def transition_to_completed(
    session: AsyncSession,
    job_id: int,
    final_verdict: str | None = None,
    findings_count: int = 0,
    github_review_id: str | None = None,
    summary: str | None = None,
    findings_data: list[dict] | str | None = None,
) -> bool:
    """Atomically marks a job as 'completed', clears lease, and records review results."""
    now = datetime.now(timezone.utc)
    values = {
        "status": "completed",
        "completed_at": now,
        "updated_at": now,
        "lease_expires_at": None,
    }
    if final_verdict is not None:
        values["final_verdict"] = final_verdict
    if findings_count:
        values["findings_count"] = findings_count
    if github_review_id is not None:
        values["github_review_id"] = github_review_id
    if summary is not None:
        values["summary"] = summary
    if findings_data is not None:
        if isinstance(findings_data, list):
            values["findings_data"] = json.dumps(findings_data)
        else:
            values["findings_data"] = str(findings_data)

    stmt = (
        update(ReviewJob)
        .where(ReviewJob.id == job_id)
        .values(**values)
    )
    await session.execute(stmt)
    await session.commit()
    logger.info(
        "Job completed: job_id=%s marked as completed (verdict=%s, findings=%s, review_id=%s)",
        job_id,
        final_verdict,
        findings_count,
        github_review_id,
    )
    return True


async def transition_to_failed(
    session: AsyncSession, job_id: int, error_message: str | None = None
) -> bool:
    """Atomically marks a job as 'failed', sanitizing error information and clearing lease."""
    now = datetime.now(timezone.utc)
    clean_error = sanitize_error(error_message)
    stmt = (
        update(ReviewJob)
        .where(ReviewJob.id == job_id)
        .values(
            status="failed",
            failed_at=now,
            updated_at=now,
            lease_expires_at=None,
            error_message=clean_error,
        )
    )
    await session.execute(stmt)
    await session.commit()
    logger.info(
        "Job failed: job_id=%s marked as failed (error=%s)",
        job_id,
        clean_error[:100] if clean_error else "None",
    )
    return True


async def fail_and_schedule_retry(
    session: AsyncSession,
    job_id: int,
    error_message: str | None = None,
    base_delay: float | None = None,
    max_attempts: int | None = None,
    exact_delay: float | None = None,
) -> tuple[bool, ReviewJob | None]:
    """
    Marks a job as 'failed' and, if under max_attempts, schedules a new attempt
    with exponential backoff delay (or exact_delay if specified, e.g. from rate-limit headers).
    """
    now = datetime.now(timezone.utc)
    clean_error = sanitize_error(error_message)
    delay_base = base_delay if base_delay is not None else settings.JOB_RETRY_BASE_DELAY
    limit_attempts = max_attempts if max_attempts is not None else settings.MAX_JOB_ATTEMPTS

    stmt = select(ReviewJob).where(ReviewJob.id == job_id)
    res = await session.execute(stmt)
    job = res.scalars().first()
    if not job:
        return False, None

    job.status = "failed"
    job.failed_at = now
    job.updated_at = now
    job.lease_expires_at = None
    job.error_message = clean_error

    retry_job = None
    if job.attempt < limit_attempts:
        if exact_delay is not None:
            delay = exact_delay
        else:
            delay = delay_base * (2 ** (job.attempt - 1))
        next_retry = now + timedelta(seconds=delay)
        retry_job = ReviewJob(
            delivery_id=None,
            repo_full_name=job.repo_full_name,
            pr_number=job.pr_number,
            head_sha=job.head_sha,
            status="queued",
            attempt=job.attempt + 1,
            next_retry_at=next_retry,
            created_at=now,
            updated_at=now,
        )
        session.add(retry_job)
        logger.info(
            "Scheduled retry for %s: attempt %s at %s (delay %.1fs)",
            job.review_key,
            retry_job.attempt,
            next_retry.isoformat(),
            delay,
        )
    else:
        logger.warning(
            "Job %s reached MAX_JOB_ATTEMPTS (%s); permanently failed",
            job.review_key,
            limit_attempts,
        )

    await session.commit()
    if retry_job is not None:
        await session.refresh(retry_job)

    return True, retry_job


async def claim_next_job(
    session: AsyncSession,
    worker_id: str,
    lease_timeout: float | None = None,
) -> ReviewJob | None:
    """
    Atomically claims the next eligible queued job using SELECT ... FOR UPDATE SKIP LOCKED.
    Safe against multiple worker processes racing for jobs.
    """
    now = datetime.now(timezone.utc)
    timeout = lease_timeout if lease_timeout is not None else settings.JOB_LEASE_TIMEOUT

    stmt = (
        select(ReviewJob)
        .where(
            ReviewJob.status == "queued",
            or_(
                ReviewJob.next_retry_at.is_(None),
                ReviewJob.next_retry_at <= now,
            ),
        )
        .order_by(ReviewJob.created_at.asc(), ReviewJob.id.asc())
        .limit(1)
    )

    bind = session.bind
    if bind is not None and bind.dialect.name != "sqlite":
        stmt = stmt.with_for_update(skip_locked=True)

    result = await session.execute(stmt)
    job = result.scalars().first()

    if not job:
        return None

    job.status = "in_progress"
    job.worker_id = worker_id
    job.started_at = now
    job.updated_at = now
    job.lease_expires_at = now + timedelta(seconds=timeout)

    await session.commit()
    await session.refresh(job)

    logger.info(
        "Worker %s claimed job_id=%s for %s (attempt %s)",
        worker_id,
        job.id,
        job.review_key,
        job.attempt,
    )
    return job


async def update_job_heartbeat(
    session: AsyncSession,
    job_id: int,
    worker_id: str,
    lease_timeout: float | None = None,
) -> bool:
    """
    Refreshes updated_at and extends lease_expires_at for an active job.
    Called periodically by a running worker.
    """
    now = datetime.now(timezone.utc)
    timeout = lease_timeout if lease_timeout is not None else settings.JOB_LEASE_TIMEOUT

    stmt = (
        update(ReviewJob)
        .where(
            ReviewJob.id == job_id,
            ReviewJob.status == "in_progress",
            ReviewJob.worker_id == worker_id,
        )
        .values(
            updated_at=now,
            lease_expires_at=now + timedelta(seconds=timeout),
        )
    )
    result = await session.execute(stmt)
    await session.commit()
    return result.rowcount > 0


async def recover_stale_jobs(
    session: AsyncSession,
    lease_timeout: float | None = None,
    schedule_retry: bool = True,
    base_delay: float | None = None,
) -> list[int]:
    """
    Scans for in_progress jobs whose lease has expired or queued jobs that were
    abandoned, marks them failed, and if eligible, schedules a retry.
    """
    timeout = lease_timeout if lease_timeout is not None else settings.JOB_LEASE_TIMEOUT
    delay_base = base_delay if base_delay is not None else settings.JOB_RETRY_BASE_DELAY
    now = datetime.now(timezone.utc)

    stmt_progress = select(ReviewJob).where(
        ReviewJob.status == "in_progress",
        or_(
            ReviewJob.lease_expires_at < now,
            and_(
                ReviewJob.lease_expires_at.is_(None),
                ReviewJob.started_at < (now - timedelta(seconds=timeout)),
            ),
        ),
    )
    res_progress = await session.execute(stmt_progress)
    stale_in_progress = res_progress.scalars().all()

    # Also detect queued jobs stuck longer than lease timeout without retry scheduling
    queued_timeout = max(timeout, 10.0)
    stmt_queued = select(ReviewJob).where(
        ReviewJob.status == "queued",
        ReviewJob.created_at < (now - timedelta(seconds=queued_timeout)),
        ReviewJob.next_retry_at.is_(None),
    )
    res_queued = await session.execute(stmt_queued)
    stale_queued = res_queued.scalars().all()

    recovered_ids = []
    for job in list(stale_in_progress) + list(stale_queued):
        recovered_ids.append(job.id)
        job.status = "failed"
        job.failed_at = now
        job.updated_at = now
        job.lease_expires_at = None
        job.error_message = (
            f"Job lease expired after {timeout}s (stale worker recovery)"
        )
        logger.warning(
            "Stale job recovered: job_id=%s, review_key=%s, attempt=%s",
            job.id,
            job.review_key,
            job.attempt,
        )

        if schedule_retry and job.attempt < settings.MAX_JOB_ATTEMPTS:
            delay = delay_base * (2 ** (job.attempt - 1))
            retry_job = ReviewJob(
                delivery_id=None,
                repo_full_name=job.repo_full_name,
                pr_number=job.pr_number,
                head_sha=job.head_sha,
                status="queued",
                attempt=job.attempt + 1,
                next_retry_at=now + timedelta(seconds=delay),
                created_at=now,
                updated_at=now,
            )
            session.add(retry_job)

    if recovered_ids:
        await session.commit()

    return recovered_ids


async def get_job(session: AsyncSession, job_id: int) -> ReviewJob | None:
    """Loads a job by primary key ID."""
    stmt = select(ReviewJob).where(ReviewJob.id == job_id)
    res = await session.execute(stmt)
    return res.scalars().first()


async def get_latest_job(
    session: AsyncSession, repo_full_name: str, pr_number: int, head_sha: str
) -> ReviewJob | None:
    """Loads the latest job for a review key."""
    stmt = (
        select(ReviewJob)
        .where(
            ReviewJob.repo_full_name == repo_full_name,
            ReviewJob.pr_number == pr_number,
            ReviewJob.head_sha == head_sha,
        )
        .order_by(ReviewJob.attempt.desc(), ReviewJob.id.desc())
    )
    res = await session.execute(stmt)
    return res.scalars().first()


async def get_recent_jobs(session: AsyncSession, limit: int = 50) -> list[ReviewJob]:
    """Loads recent jobs ordered by id descending."""
    stmt = select(ReviewJob).order_by(ReviewJob.id.desc()).limit(limit)
    res = await session.execute(stmt)
    return list(res.scalars().all())


async def retry_failed_job(
    session: AsyncSession, job_id: int
) -> tuple[bool, str, ReviewJob | None]:
    """
    Manually retries a failed review job (administrative/operator endpoint).
    Returns (True, message, new_job) if retry is queued.
    Returns (False, reason, None) if job is not found or not in failed status.
    """
    job = await get_job(session, job_id)
    if not job:
        return False, f"Job {job_id} not found", None

    if job.status != "failed":
        return False, f"Cannot retry job {job_id} with status '{job.status}'. Only 'failed' jobs can be retried.", None

    now = datetime.now(timezone.utc)
    next_attempt = job.attempt + 1

    retry_job = ReviewJob(
        delivery_id=None,
        repo_full_name=job.repo_full_name,
        pr_number=job.pr_number,
        head_sha=job.head_sha,
        status="queued",
        attempt=next_attempt,
        next_retry_at=None,
        created_at=now,
        updated_at=now,
    )
    session.add(retry_job)
    job.next_retry_at = None
    await session.flush()
    await session.commit()

    logger.info(
        "Manual retry scheduled for job_id=%s -> new job_id=%s (attempt %s) on %s",
        job.id,
        retry_job.id,
        next_attempt,
        retry_job.review_key,
    )
    return True, "Job scheduled for immediate retry", retry_job


async def get_job_stats(session: AsyncSession) -> dict:
    """Returns aggregated queue and review counts by status."""
    from sqlalchemy import func
    stmt = select(ReviewJob.status, func.count(ReviewJob.id)).group_by(ReviewJob.status)
    res = await session.execute(stmt)
    status_counts = dict(res.all())

    queued = status_counts.get("queued", 0)
    in_progress = status_counts.get("in_progress", 0)
    completed = status_counts.get("completed", 0)
    failed = status_counts.get("failed", 0)
    total = sum(status_counts.values())

    return {
        "total_jobs": total,
        "queued": queued,
        "in_progress": in_progress,
        "completed": completed,
        "failed": failed,
    }


async def reset_in_progress_job_to_queued(
    session: AsyncSession, job_id: int, worker_id: str
) -> bool:
    """
    Safely resets an in_progress job back to 'queued' when a worker shuts down gracefully
    or is interrupted before completion, clearing the worker_id and lease so other
    workers (or the restarted worker) can claim it immediately without waiting 300s.
    """
    now = datetime.now(timezone.utc)
    stmt = (
        update(ReviewJob)
        .where(
            ReviewJob.id == job_id,
            ReviewJob.status == "in_progress",
            ReviewJob.worker_id == worker_id,
        )
        .values(
            status="queued",
            worker_id=None,
            lease_expires_at=None,
            started_at=None,
            updated_at=now,
        )
    )
    result = await session.execute(stmt)
    await session.commit()
    if result.rowcount > 0:
        logger.info(
            "Job reset to queued: job_id=%s released by worker %s on shutdown",
            job_id,
            worker_id,
        )
        return True
    return False

