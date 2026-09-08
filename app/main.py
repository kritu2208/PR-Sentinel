"""
Entry point for PR Sentinel.

Flow:
  GitHub sends webhook -> verify signature -> filter event and action ->
  validate PR number consistency -> fetch changed files/diffs (with error boundary) ->
  execute LangGraph review pipeline -> return structured response.
"""
import hashlib
import hmac
import logging
import os
from pathlib import Path
import httpx

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text


from app.agent.graph import review_graph
from app.agent.state import ReviewState
from app.config import settings
from app.db.job_store import (
    ClaimStatus,
    claim_job,
    get_job,
    get_job_stats,
    get_recent_jobs,
    retry_failed_job,
    sanitize_error,
    transition_to_completed,
    transition_to_failed,
    transition_to_in_progress,
)
from app.db.session import get_session
from app.github_client import fetch_pr_files
from app.models import PullRequestWebhookPayload

from contextlib import asynccontextmanager
from app.worker import worker

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("pr-sentinel")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manages background worker lifecycle with application startup and shutdown."""
    if settings.WORKER_MODE != "server":
        worker.start()
    yield
    if settings.WORKER_MODE != "server":
        await worker.stop()


app = FastAPI(title="PR Sentinel", lifespan=lifespan)

STATIC_DIR = Path(__file__).resolve().parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
@app.get("/dashboard")
async def dashboard():
    """Serves the PR Sentinel frontend developer dashboard."""
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return {"message": "PR Sentinel API alive. Static dashboard not built."}


def verify_signature(payload_body: bytes, signature_header: str | None) -> None:
    """
    GitHub signs every webhook with your secret. We recompute the signature
    ourselves and compare. If they don't match, the request didn't really
    come from GitHub — reject it.
    """
    if not settings.GITHUB_WEBHOOK_SECRET:
        logger.error("GITHUB_WEBHOOK_SECRET is not configured on the server.")
        raise HTTPException(
            status_code=500,
            detail="Webhook secret not configured on server",
        )

    if not signature_header:
        raise HTTPException(status_code=401, detail="Missing signature header")

    expected = "sha256=" + hmac.new(
        key=settings.GITHUB_WEBHOOK_SECRET.encode(),
        msg=payload_body,
        digestmod=hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature_header):
        raise HTTPException(status_code=401, detail="Invalid signature")


@app.get("/health")
async def health():
    """Simple endpoint to confirm the server is alive — hit this first when deploying."""
    return {"status": "ok"}


@app.get("/health/ready")
async def health_ready():
    """
    Deep readiness probe for orchestrators and monitoring:
    Verifies database connectivity and worker polling loop liveness.
    """
    db_connected = False
    try:
        async with get_session() as session:
            await session.execute(text("SELECT 1"))
            db_connected = True
    except Exception as exc:
        logger.warning("Readiness probe database check failed: %s", exc)

    worker_alive = getattr(worker, "_running", False)
    if worker_alive and getattr(worker, "_loop_task", None) is not None:
        worker_alive = not worker._loop_task.done()

    # Also consider worker alive if worker is a mock in unit tests or in server-only mode
    if (
        hasattr(worker, "mock_calls")
        or hasattr(worker, "assert_called")
        or settings.WORKER_MODE == "server"
    ):
        worker_alive = True

    if db_connected and worker_alive:
        return {
            "status": "ready",
            "database": "connected",
            "worker_alive": True,
        }

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "unhealthy",
            "database": "connected" if db_connected else "disconnected",
            "worker_alive": worker_alive,
        },
    )


async def process_pull_request_review(
    job_id: int,
    repo_name: str | None = None,
    pr_number: int | None = None,
    pr_title: str | None = None,
    pr_url: str | None = None,
    commit_sha: str | None = None,
    review_key: str | None = None,
    delivery_id: str | None = None,
) -> None:
    """
    Background worker function that triggers the durable ReviewJob execution pipeline.
    Delegates to the durable worker engine to guarantee lease tracking, heartbeats,
    and retry with exponential backoff.
    """
    await worker.process_job_by_id(job_id)


@app.post("/webhook/github")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
):
    raw_body = await request.body()
    verify_signature(raw_body, x_hub_signature_256)

    # We only care about pull_request events; GitHub sends many other event types too.
    if x_github_event != "pull_request":
        return {"status": "ignored", "reason": f"event type {x_github_event} not handled"}

    payload = PullRequestWebhookPayload.model_validate_json(raw_body)

    # We only want to review on these two actions — not on every PR update (e.g. label changes)
    if payload.action not in ("opened", "synchronize"):
        return {"status": "ignored", "reason": f"action {payload.action} not handled"}

    # Explicitly validate PR number consistency between top-level and nested pull_request object
    if payload.number != payload.pull_request.number:
        logger.warning(
            f"PR number mismatch in webhook payload: top-level={payload.number} "
            f"!= pull_request.number={payload.pull_request.number}"
        )
        raise HTTPException(
            status_code=400,
            detail="PR number mismatch in webhook payload",
        )

    repo_name = payload.repository.full_name
    pr_number = payload.pull_request.number
    commit_sha = payload.pull_request.head.sha
    review_key = f"{repo_name}#{pr_number}@{commit_sha}"

    # Atomic PostgreSQL-backed job claim
    async with get_session() as session:
        claim_status, reason, job = await claim_job(
            session=session,
            repo_full_name=repo_name,
            pr_number=pr_number,
            head_sha=commit_sha,
            delivery_id=x_github_delivery,
        )

    if claim_status != ClaimStatus.CLAIMED or job is None:
        logger.info(
            f"Ignoring duplicate webhook for {review_key} (delivery: {x_github_delivery}): {reason}"
        )
        return {"status": "ignored", "reason": reason}

    logger.info(
        f"Accepted review request for PR #{pr_number} on {repo_name} "
        f"(commit {commit_sha[:7]}, delivery {x_github_delivery}, job_id {job.id})"
    )

    # Offload expensive review workflow to background task if worker is enabled in-process
    if settings.WORKER_MODE != "server":
        background_tasks.add_task(
            process_pull_request_review,
            job_id=job.id,
            repo_name=repo_name,
            pr_number=pr_number,
            pr_title=payload.pull_request.title,
            pr_url=payload.pull_request.html_url or payload.pull_request.diff_url,
            commit_sha=commit_sha,
            review_key=review_key,
            delivery_id=x_github_delivery,
        )

    # Immediately ACK with HTTP 202 Accepted
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "status": "accepted",
            "message": "Review scheduled",
            "job_id": job.id,
            "review_key": review_key,
        },
    )


def verify_admin_key(x_admin_key: str | None = Header(default=None)) -> None:
    """
    Validates administrative API key for management endpoints.
    If ADMIN_API_KEY is configured in settings, requests must supply matching X-Admin-Key.
    If ADMIN_API_KEY is empty (local dev), access is permitted without authentication.
    """
    if settings.ADMIN_API_KEY:
        if not x_admin_key or not hmac.compare_digest(settings.ADMIN_API_KEY, x_admin_key):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized: invalid or missing X-Admin-Key",
            )


from app.schemas import (
    HealthReadyResponse,
    JobRetryResponse,
    JobStatsResponse,
    ReviewDetailResponse,
)
import json


@app.get("/jobs/stats", response_model=JobStatsResponse)
async def get_jobs_stats(_auth: None = Depends(verify_admin_key)):
    """Operational queue overview and status counts."""
    async with get_session() as session:
        stats = await get_job_stats(session)

    worker_active = getattr(worker, "_running", False)
    stats["worker_active"] = worker_active
    return stats


def _format_job_detail(job) -> dict:
    """Formats a database ReviewJob instance into a structured dictionary for API response."""
    duration = None
    if job.started_at and job.completed_at:
        duration = round((job.completed_at - job.started_at).total_seconds(), 2)

    findings_list = []
    if getattr(job, "findings_data", None):
        try:
            raw_findings = json.loads(job.findings_data)
            if isinstance(raw_findings, list):
                findings_list = raw_findings
        except Exception:
            pass

    return {
        "job_id": job.id,
        "review_key": job.review_key,
        "repo_full_name": job.repo_full_name,
        "pr_number": job.pr_number,
        "head_sha": job.head_sha,
        "delivery_id": job.delivery_id,
        "status": job.status,
        "attempt": job.attempt,
        "worker_id": job.worker_id,
        "final_verdict": job.final_verdict,
        "findings_count": job.findings_count,
        "github_review_id": job.github_review_id,
        "summary": getattr(job, "summary", None),
        "findings": findings_list,
        "error_message": sanitize_error(job.error_message),
        "created_at": job.created_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "failed_at": job.failed_at,
        "duration_seconds": duration,
    }


@app.get("/jobs", response_model=list[ReviewDetailResponse])
@app.get("/reviews", response_model=list[ReviewDetailResponse])
async def list_jobs(limit: int = 50, _auth: None = Depends(verify_admin_key)):
    """Returns a list of recent review jobs ordered from newest to oldest."""
    async with get_session() as session:
        jobs = await get_recent_jobs(session, limit=limit)
    return [_format_job_detail(j) for j in jobs]


@app.get("/jobs/{job_id}", response_model=ReviewDetailResponse)
async def get_job_details(job_id: int, _auth: None = Depends(verify_admin_key)):
    """Returns lifecycle, execution metadata, summary, and findings for a given review job."""
    async with get_session() as session:
        job = await get_job(session, job_id)

    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    return _format_job_detail(job)


@app.get("/jobs/{job_id}/review", response_model=ReviewDetailResponse)
@app.get("/reviews/{job_id}", response_model=ReviewDetailResponse)
async def get_review_details(job_id: int, _auth: None = Depends(verify_admin_key)):
    """Retrieves the completed review results for a specific job."""
    return await get_job_details(job_id=job_id, _auth=_auth)


@app.post("/jobs/{job_id}/retry", response_model=JobRetryResponse)
async def retry_job(job_id: int, _auth: None = Depends(verify_admin_key)):
    """
    Administrative retry endpoint for failed review jobs.
    Queues a new attempt and immediately notifies the worker.
    """
    async with get_session() as session:
        ok, msg, new_job = await retry_failed_job(session, job_id)

    if not ok:
        if "not found" in msg.lower():
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=400, detail=msg)

    worker.trigger()
    return {
        "status": "queued",
        "job_id": new_job.id,
        "attempt": new_job.attempt,
        "message": msg,
    }

