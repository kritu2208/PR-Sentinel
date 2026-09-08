"""
SQLAlchemy models for PR Sentinel persistent job state and idempotency.
"""
from datetime import datetime
from sqlalchemy import (
    DateTime,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ReviewJob(Base):
    __tablename__ = "review_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    delivery_id: Mapped[str | None] = mapped_column(
        String(128), unique=True, nullable=True, index=True
    )
    repo_full_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued", index=True
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Worker ownership, lease tracking, and backoff retry fields
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    # Review execution results & GitHub metadata
    final_verdict: Mapped[str | None] = mapped_column(String(32), nullable=True)
    findings_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    github_review_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    findings_data: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index(
            "uq_active_or_completed_review",
            "repo_full_name",
            "pr_number",
            "head_sha",
            unique=True,
            postgresql_where=(status.in_(["queued", "in_progress", "completed"])),
            sqlite_where=(status.in_(["queued", "in_progress", "completed"])),
        ),
        Index("ix_review_jobs_claim_lookup", "status", "next_retry_at", "created_at"),
    )


    @property
    def review_key(self) -> str:
        return f"{self.repo_full_name}#{self.pr_number}@{self.head_sha}"

    def __repr__(self) -> str:
        return (
            f"<ReviewJob(id={self.id}, review_key={self.review_key}, "
            f"delivery={self.delivery_id}, status={self.status}, attempt={self.attempt})>"
        )
