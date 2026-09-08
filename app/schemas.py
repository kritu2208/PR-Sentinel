"""
Pydantic API response schemas for PR Sentinel REST platform.
"""
from datetime import datetime
from pydantic import BaseModel, Field

from app.agent.schemas import Finding


class HealthReadyResponse(BaseModel):
    status: str = Field(..., description="Service health state: 'ready' or 'unhealthy'.")
    database: str = Field(..., description="Database connection state: 'connected' or 'disconnected'.")
    worker_alive: bool = Field(..., description="Whether worker polling loop is active.")


class JobStatsResponse(BaseModel):
    total_jobs: int = Field(..., description="Total review jobs recorded.")
    queued: int = Field(..., description="Count of queued review jobs.")
    in_progress: int = Field(..., description="Count of in-progress review jobs.")
    completed: int = Field(..., description="Count of completed review jobs.")
    failed: int = Field(..., description="Count of failed review jobs.")
    worker_active: bool = Field(..., description="Whether background worker engine is active.")


class JobRetryResponse(BaseModel):
    status: str = Field(default="queued", description="New status after operator retry.")
    job_id: int = Field(..., description="ID of the newly queued job attempt.")
    attempt: int = Field(..., description="Attempt count for the review.")
    message: str = Field(..., description="Human-readable retry status message.")


class ReviewDetailResponse(BaseModel):
    job_id: int = Field(..., description="Unique job identifier.")
    review_key: str = Field(..., description="Unique review identifier formatted as repo#pr@sha.")
    repo_full_name: str = Field(..., description="Repository full name (e.g. 'owner/repo').")
    pr_number: int = Field(..., description="Pull request number.")
    head_sha: str = Field(..., description="Commit SHA under review.")
    delivery_id: str | None = Field(default=None, description="GitHub webhook delivery ID.")
    status: str = Field(..., description="Review status ('queued', 'in_progress', 'completed', 'failed').")
    attempt: int = Field(..., description="Current execution attempt number.")
    worker_id: str | None = Field(default=None, description="Worker identifier holding the lease.")
    final_verdict: str | None = Field(default=None, description="Review verdict: 'approve', 'comment', or 'request_changes'.")
    findings_count: int = Field(default=0, description="Total number of findings identified.")
    github_review_id: str | None = Field(default=None, description="Official GitHub Pull Request Review ID.")
    summary: str | None = Field(default=None, description="Executive markdown review summary.")
    findings: list[Finding] = Field(default_factory=list, description="Structured review findings with investigation details.")
    error_message: str | None = Field(default=None, description="Sanitized failure error message without secrets or stack traces.")
    created_at: datetime | None = Field(default=None, description="Job creation timestamp.")
    started_at: datetime | None = Field(default=None, description="Job execution start timestamp.")
    completed_at: datetime | None = Field(default=None, description="Job completion timestamp.")
    failed_at: datetime | None = Field(default=None, description="Job failure timestamp.")
    duration_seconds: float | None = Field(default=None, description="Total execution duration in seconds.")
