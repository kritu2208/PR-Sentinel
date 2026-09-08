"""
Tests for Phase 6 Operational & Observability API Endpoints:
- GET /health/ready (Deep readiness probe)
- GET /jobs/{job_id} (Lifecycle status and results)
- POST /jobs/{job_id}/retry (Manual operator retry)
- GET /jobs/stats (Queue metrics)
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
import uuid
import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.db.job_store import claim_job, get_job, transition_to_completed, transition_to_in_progress
from app.db.session import get_session
from app.main import app
from app.worker import worker



@pytest.mark.asyncio
async def test_api_health_ready_healthy():
    """GET /health/ready returns 200 when DB is reachable and worker is alive."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        with patch.object(worker, "_running", True), patch.object(worker, "_loop_task", None):
            resp = await ac.get("/health/ready")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ready"
            assert data["database"] == "connected"
            assert data["worker_alive"] is True


@pytest.mark.asyncio
async def test_api_health_ready_db_failure():
    """GET /health/ready returns 503 when database ping fails."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        with patch("app.main.get_session", side_effect=Exception("Database down")):
            resp = await ac.get("/health/ready")
            assert resp.status_code == 503
            data = resp.json()
            assert data["status"] == "unhealthy"
            assert data["database"] == "disconnected"


@pytest.mark.asyncio
async def test_api_get_job_not_found():
    """GET /jobs/{job_id} returns 404 for non-existent job ID."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/jobs/999999")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_api_get_job_completed_details():
    """GET /jobs/{job_id} returns full job details, final verdict, review ID, and duration."""
    uid = uuid.uuid4().hex[:8]
    job_id = None
    async with get_session() as session:
        _, _, job = await claim_job(
            session, "octocat/Hello-World", 301, f"sha_api_{uid}", delivery_id=f"deliv-api-{uid}"
        )
        job_id = job.id
        await transition_to_in_progress(session, job_id, worker_id="test-worker", lease_timeout=300)
        # Fast-forward started_at to test duration calculation
        now = datetime.now(timezone.utc)
        j = await get_job(session, job_id)
        j.started_at = now - timedelta(seconds=12)
        await session.commit()

        await transition_to_completed(
            session=session,
            job_id=job_id,
            final_verdict="request_changes",
            findings_count=3,
            github_review_id="gh-rev-777",
        )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get(f"/jobs/{job_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["job_id"] == job_id
        assert data["repo_full_name"] == "octocat/Hello-World"
        assert data["pr_number"] == 301
        assert data["status"] == "completed"
        assert data["final_verdict"] == "request_changes"
        assert data["findings_count"] == 3
        assert data["github_review_id"] == "gh-rev-777"
        assert data["duration_seconds"] is not None
        assert data["duration_seconds"] >= 10.0


@pytest.mark.asyncio
async def test_api_retry_failed_job_succeeds():
    """POST /jobs/{job_id}/retry queues a new attempt for a failed job and returns 200."""
    uid = uuid.uuid4().hex[:8]
    failed_job_id = None
    async with get_session() as session:
        _, _, job = await claim_job(
            session, "octocat/Hello-World", 302, f"sha_retry_{uid}", delivery_id=f"deliv-retry-{uid}"
        )
        failed_job_id = job.id
        j = await get_job(session, failed_job_id)
        j.status = "failed"
        j.error_message = "Previous timeout"
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        with patch.object(worker, "trigger") as mock_trigger:
            resp = await ac.post(f"/jobs/{failed_job_id}/retry")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "queued"
            assert data["attempt"] == 2
            assert "scheduled" in data["message"].lower()
            mock_trigger.assert_called_once()


@pytest.mark.asyncio
async def test_api_retry_non_failed_job_returns_400():
    """POST /jobs/{job_id}/retry rejects retrying jobs that are completed or in-progress."""
    uid = uuid.uuid4().hex[:8]
    job_id = None
    async with get_session() as session:
        _, _, job = await claim_job(
            session, "octocat/Hello-World", 303, f"sha_nonfail_{uid}", delivery_id=f"deliv-nonfail-{uid}"
        )
        job_id = job.id
        await transition_to_in_progress(session, job_id, worker_id="test-worker")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Try retrying an in_progress job
        resp = await ac.post(f"/jobs/{job_id}/retry")
        assert resp.status_code == 400
        assert "cannot retry" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_api_jobs_stats_endpoint():
    """GET /jobs/stats returns aggregated counts of jobs grouped by status."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/jobs/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_jobs" in data
        assert "queued" in data
        assert "in_progress" in data
        assert "completed" in data
        assert "failed" in data
        assert "worker_active" in data
        assert data["total_jobs"] >= (data["queued"] + data["in_progress"] + data["completed"] + data["failed"])


@pytest.mark.asyncio
async def test_api_admin_key_authentication(monkeypatch):
    """Management endpoints enforce X-Admin-Key header when ADMIN_API_KEY is configured."""
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "prod-secret-admin-token")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 1. Missing header -> 401
        res_missing = await ac.get("/jobs/stats")
        assert res_missing.status_code == 401
        assert "unauthorized" in res_missing.json()["detail"].lower()

        # 2. Invalid header -> 401
        res_bad = await ac.get("/jobs/stats", headers={"X-Admin-Key": "wrong-token"})
        assert res_bad.status_code == 401
        assert "unauthorized" in res_bad.json()["detail"].lower()

        # 3. Correct header -> 200
        res_good = await ac.get("/jobs/stats", headers={"X-Admin-Key": "prod-secret-admin-token"})
        assert res_good.status_code == 200
        assert "total_jobs" in res_good.json()


@pytest.mark.asyncio
async def test_api_health_ready_server_mode(monkeypatch):
    """In server-only WORKER_MODE, readiness probe reports ready without requiring in-process worker loop."""
    monkeypatch.setattr(settings, "WORKER_MODE", "server")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        with patch.object(worker, "_running", False), patch.object(worker, "_loop_task", None):
            resp = await ac.get("/health/ready")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ready"
            assert data["database"] == "connected"
            assert data["worker_alive"] is True


@pytest.mark.asyncio
async def test_webhook_server_mode_decoupling(monkeypatch):
    """In server-only WORKER_MODE, webhook returns 202 and stores job in DB but does not schedule in-process BackgroundTask."""
    import hashlib
    import hmac
    import json

    monkeypatch.setattr(settings, "WORKER_MODE", "server")
    monkeypatch.setattr(settings, "GITHUB_WEBHOOK_SECRET", "test_secret_sm")

    uid = uuid.uuid4().hex[:8]
    payload = {
        "action": "opened",
        "number": 99,
        "pull_request": {
            "number": 99,
            "title": "Server Mode Test PR",
            "diff_url": "https://github.com/test/repo/pull/99.diff",
            "html_url": "https://github.com/test/repo/pull/99",
            "head": {"sha": f"sha_sm_{uid}"},
        },
        "repository": {"full_name": f"test/repo_sm_{uid}"},
    }
    body = json.dumps(payload).encode("utf-8")
    sig = "sha256=" + hmac.new(b"test_secret_sm", msg=body, digestmod=hashlib.sha256).hexdigest()

    transport = ASGITransport(app=app)
    with patch("app.main.process_pull_request_review") as mock_process:
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/webhook/github",
                content=body,
                headers={
                    "X-Hub-Signature-256": sig,
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": f"deliv-sm-{uid}",
                },
            )
            assert resp.status_code == 202
            data = resp.json()
            assert data["status"] == "accepted"
            job_id = data["job_id"]

            mock_process.assert_not_called()

            async with get_session() as session:
                job = await get_job(session, job_id)
                assert job is not None
                assert job.status == "queued"


@pytest.mark.asyncio
async def test_api_get_review_with_findings_and_investigation():
    """GET /jobs/{job_id} and GET /reviews/{job_id} return structured findings with root cause and recommendations."""
    uid = uuid.uuid4().hex[:8]
    job_id = None
    mock_findings = [
        {
            "file": "app/auth.py",
            "line": 15,
            "severity": "critical",
            "category": "security",
            "title": "JWT Signature Bypass",
            "comment": "Unverified token accepted.",
            "confidence": 0.95,
            "root_cause": "jwt.decode called with verify=False",
            "evidence": "jwt.decode(token, verify=False)",
            "impact": "Account takeover vulnerability.",
            "recommendation": "Enforce signature verification using public key.",
            "investigation_status": "confirmed",
        }
    ]
    summary_text = "## 🛡️ PR Sentinel: Changes Requested\nFound 1 critical issue."

    async with get_session() as session:
        _, _, job = await claim_job(
            session, "octocat/Hello-World", 401, f"sha_rev_{uid}", delivery_id=f"deliv-rev-{uid}"
        )
        job_id = job.id
        await transition_to_in_progress(session, job_id, worker_id="worker-rev-1")
        await transition_to_completed(
            session=session,
            job_id=job_id,
            final_verdict="request_changes",
            findings_count=1,
            github_review_id="gh-rev-888",
            summary=summary_text,
            findings_data=mock_findings,
        )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Test GET /jobs/{job_id}
        resp = await ac.get(f"/jobs/{job_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["job_id"] == job_id
        assert data["final_verdict"] == "request_changes"
        assert data["summary"] == summary_text
        assert len(data["findings"]) == 1
        finding = data["findings"][0]
        assert finding["file"] == "app/auth.py"
        assert finding["line"] == 15
        assert finding["severity"] == "critical"
        assert finding["root_cause"] == "jwt.decode called with verify=False"
        assert finding["investigation_status"] == "confirmed"
        assert finding["recommendation"] == "Enforce signature verification using public key."

        # Test GET /reviews/{job_id} alias
        resp_alias = await ac.get(f"/reviews/{job_id}")
        assert resp_alias.status_code == 200
        assert resp_alias.json()["job_id"] == job_id
        assert len(resp_alias.json()["findings"]) == 1


@pytest.mark.asyncio
async def test_api_failed_job_sanitizes_error_and_hides_secrets():
    """Failed jobs return sanitized error messages without passwords or stack traces."""
    uid = uuid.uuid4().hex[:8]
    job_id = None
    async with get_session() as session:
        _, _, job = await claim_job(
            session, "octocat/Hello-World", 402, f"sha_fail_{uid}", delivery_id=f"deliv-fail-{uid}"
        )
        job_id = job.id
        j = await get_job(session, job_id)
        j.status = "failed"
        j.error_message = "Database connection error on postgresql://admin:supersecretpassword@127.0.0.1"
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get(f"/jobs/{job_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "failed"
        assert "supersecretpassword" not in data["error_message"]


@pytest.mark.asyncio
async def test_api_list_jobs_and_reviews():
    """GET /jobs and GET /reviews return ordered list of review jobs."""
    uid = uuid.uuid4().hex[:8]
    async with get_session() as session:
        await claim_job(
            session, "octocat/Hello-World", 501, f"sha_list_1_{uid}", delivery_id=f"deliv-list-1-{uid}"
        )
        await claim_job(
            session, "octocat/Hello-World", 502, f"sha_list_2_{uid}", delivery_id=f"deliv-list-2-{uid}"
        )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/jobs?limit=10")
        assert resp.status_code == 200
        jobs = resp.json()
        assert isinstance(jobs, list)
        assert len(jobs) >= 2
        # Check reverse ID order (newest first)
        assert jobs[0]["job_id"] > jobs[1]["job_id"]

        # Check /reviews alias
        resp_reviews = await ac.get("/reviews?limit=10")
        assert resp_reviews.status_code == 200
        assert len(resp_reviews.json()) >= 2


@pytest.mark.asyncio
async def test_dashboard_ui_and_static_files():
    """GET / and GET /dashboard serve HTML dashboard and static CSS/JS."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp_root = await ac.get("/")
        assert resp_root.status_code == 200
        assert "PR Sentinel" in resp_root.text
        assert "PR SENTINEL" in resp_root.text

        resp_dash = await ac.get("/dashboard")
        assert resp_dash.status_code == 200
        assert "Finding Severity Breakdown" in resp_dash.text

        resp_css = await ac.get("/static/style.css")
        assert resp_css.status_code == 200
        assert "--sev-critical" in resp_css.text

        resp_js = await ac.get("/static/app.js")
        assert resp_js.status_code == 200
        assert "ApiClient" in resp_js.text
