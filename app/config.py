"""
Central place for all environment configuration.
Keeping this separate means every other file just does `from app.config import settings`
instead of scattering os.environ.get() calls everywhere.
"""
import os
from dotenv import load_dotenv

load_dotenv()  # reads .env file in local dev; in production these come from the host's env vars


class Settings:
    # Personal Access Token for now (simplest to get started).
    # Later, upgrade path: replace with a GitHub App private key + JWT auth,
    # which lets the product be installed by OTHER people's repos, not just yours.
    GITHUB_TOKEN: str = os.environ.get("GITHUB_TOKEN", "")

    # Secret you set when creating the GitHub webhook. Used to verify that
    # incoming requests really came from GitHub and not some random POST.
    GITHUB_WEBHOOK_SECRET: str = os.environ.get("GITHUB_WEBHOOK_SECRET", "")

    # Which LLM provider we call in the agent pipeline.
    GROQ_API_KEY: str = os.environ.get("GROQ_API_KEY", "")

    # Model name hosted on Groq - configurable via env var
    GROQ_MODEL: str = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

    # Sensible limits for file and diff processing
    MAX_FILES_TO_REVIEW: int = int(os.environ.get("MAX_FILES_TO_REVIEW", "15"))
    MAX_PATCH_BYTES: int = int(os.environ.get("MAX_PATCH_BYTES", "20000"))
    MIN_CONFIDENCE_THRESHOLD: float = float(os.environ.get("MIN_CONFIDENCE_THRESHOLD", "0.7"))

    GITHUB_API_BASE: str = "https://api.github.com"
    GITHUB_REQUEST_TIMEOUT: float = float(os.environ.get("GITHUB_REQUEST_TIMEOUT", "15.0"))

    # Database configuration for persistent job and idempotency storage
    DATABASE_URL: str = os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/pr_sentinel"
    )

    # Maximum lease duration (seconds) before an in_progress or queued job is considered stale
    JOB_LEASE_TIMEOUT: float = float(os.environ.get("JOB_LEASE_TIMEOUT", "300.0"))

    # Worker retry & polling configuration
    MAX_JOB_ATTEMPTS: int = int(os.environ.get("MAX_JOB_ATTEMPTS", "3"))
    JOB_RETRY_BASE_DELAY: float = float(os.environ.get("JOB_RETRY_BASE_DELAY", "2.0"))
    JOB_POLL_INTERVAL: float = float(os.environ.get("JOB_POLL_INTERVAL", "1.0"))
    WORKER_HEARTBEAT_INTERVAL: float = float(
        os.environ.get("WORKER_HEARTBEAT_INTERVAL", "10.0")
    )
    SUBMIT_OFFICIAL_PR_REVIEW: bool = os.environ.get(
        "SUBMIT_OFFICIAL_PR_REVIEW", "true"
    ).lower() in ("true", "1", "yes")

    # Phase 6 operational and safety settings
    ADMIN_API_KEY: str = os.environ.get("ADMIN_API_KEY", "")
    MAX_INLINE_COMMENTS_PER_REVIEW: int = int(
        os.environ.get("MAX_INLINE_COMMENTS_PER_REVIEW", "25")
    )
    ENABLE_COMMIT_STATUS: bool = os.environ.get(
        "ENABLE_COMMIT_STATUS", "false"
    ).lower() in ("true", "1", "yes")
    WORKER_SHUTDOWN_TIMEOUT: float = float(
        os.environ.get("WORKER_SHUTDOWN_TIMEOUT", "15.0")
    )

    # Worker execution mode:
    # "hybrid": Web server runs both webhook receiver and in-process worker loop / background tasks (default)
    # "server": Web server runs only webhook receiver and management API; does not run in-process worker
    # "worker": Dedicated worker process (no web server)
    WORKER_MODE: str = os.environ.get("WORKER_MODE", "hybrid").lower()

    # Phase 7 Codebase-Aware Retrieval settings
    ENABLE_CODEBASE_RETRIEVAL: bool = os.environ.get(
        "ENABLE_CODEBASE_RETRIEVAL", "true"
    ).lower() in ("true", "1", "yes")
    MAX_RETRIEVED_FILES: int = int(os.environ.get("MAX_RETRIEVED_FILES", "5"))
    MAX_RETRIEVED_SNIPPET_BYTES: int = int(
        os.environ.get("MAX_RETRIEVED_SNIPPET_BYTES", "2000")
    )
    MAX_TOTAL_RETRIEVAL_BYTES: int = int(
        os.environ.get("MAX_TOTAL_RETRIEVAL_BYTES", "8000")
    )


settings = Settings()

