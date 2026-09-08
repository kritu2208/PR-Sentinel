"""
Integration tests for PR Sentinel FastAPI webhook and LangGraph pipeline.
All external network interactions (Groq LLM and GitHub API) are strictly mocked.
"""
import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch
import pytest
import httpx
from httpx import ASGITransport, AsyncClient

from app.agent.graph import review_graph
from app.agent.idempotency import idempotency_store
from app.agent.schemas import AnalyzerOutput, Finding
from app.config import settings
from app.main import app


@pytest.fixture(autouse=True)
def reset_idempotency_store():
    """Ensures each test starts with a clean idempotency store."""
    idempotency_store.clear()
    yield
    idempotency_store.clear()


def _compute_signature(secret: str, body: bytes) -> str:
    """Computes HMAC-SHA256 signature for test payload."""
    sig = hmac.new(secret.encode(), msg=body, digestmod=hashlib.sha256).hexdigest()
    return f"sha256={sig}"


SAMPLE_PR_PAYLOAD = {
    "action": "opened",
    "number": 7,
    "pull_request": {
        "number": 7,
        "title": "Add user authentication endpoint",
        "diff_url": "https://github.com/octocat/Hello-World/pull/7.diff",
        "html_url": "https://github.com/octocat/Hello-World/pull/7",
        "head": {"sha": "6dcb09b5b57875f334f61aebed695e2e4193db5e"},
    },
    "repository": {"full_name": "octocat/Hello-World"},
}


@pytest.mark.asyncio
async def test_health_endpoint():
    """Confirms /health returns status ok."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_webhook_rejects_missing_signature(monkeypatch):
    """Webhook rejects payload when signature header is missing."""
    monkeypatch.setattr(settings, "GITHUB_WEBHOOK_SECRET", "test_secret_123")
    body = json.dumps(SAMPLE_PR_PAYLOAD).encode("utf-8")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.post(
            "/webhook/github",
            content=body,
            headers={"X-GitHub-Event": "pull_request"},
        )
    assert response.status_code == 401
    assert response.json()["detail"] == "Missing signature header"


@pytest.mark.asyncio
async def test_webhook_rejects_invalid_signature(monkeypatch):
    """Webhook rejects payload with invalid signature."""
    monkeypatch.setattr(settings, "GITHUB_WEBHOOK_SECRET", "test_secret_123")
    body = json.dumps(SAMPLE_PR_PAYLOAD).encode("utf-8")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.post(
            "/webhook/github",
            content=body,
            headers={
                "X-Hub-Signature-256": "sha256=invalidhexsignature",
                "X-GitHub-Event": "pull_request",
            },
        )
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid signature"


@pytest.mark.asyncio
async def test_webhook_ignores_unsupported_event(monkeypatch):
    """Webhook ignores non-pull_request events with a valid signature."""
    secret = "test_secret_123"
    monkeypatch.setattr(settings, "GITHUB_WEBHOOK_SECRET", secret)
    body = json.dumps({"zen": "Keep it simple"}).encode("utf-8")
    sig = _compute_signature(secret, body)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.post(
            "/webhook/github",
            content=body,
            headers={
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "ping",
            },
        )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ignored"
    assert "ping" in data["reason"]


@pytest.mark.asyncio
async def test_webhook_ignores_unhandled_action(monkeypatch):
    """Webhook ignores PR actions other than 'opened' and 'synchronize'."""
    secret = "test_secret_123"
    monkeypatch.setattr(settings, "GITHUB_WEBHOOK_SECRET", secret)
    payload = dict(SAMPLE_PR_PAYLOAD)
    payload["action"] = "closed"
    body = json.dumps(payload).encode("utf-8")
    sig = _compute_signature(secret, body)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.post(
            "/webhook/github",
            content=body,
            headers={
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "pull_request",
            },
        )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ignored"
    assert "closed" in data["reason"]


@pytest.mark.asyncio
async def test_webhook_successful_review_execution(monkeypatch):
    """
    End-to-end test: Webhook receives valid signed PR payload, returns HTTP 202 Accepted,
    and executes LangGraph review pipeline in BackgroundTasks.
    """
    secret = "test_secret_123"
    monkeypatch.setattr(settings, "GITHUB_WEBHOOK_SECRET", secret)
    monkeypatch.setattr(settings, "GROQ_API_KEY", "mock-groq-key")

    mock_diff_files = [
        {
            "filename": "app/auth.py",
            "status": "modified",
            "additions": 5,
            "deletions": 1,
            "patch": "@@ -1,5 +1,6 @@\n def login(u, p):\n+    query = f'SELECT * FROM users WHERE u={u}'\n     return query\n",
        }
    ]

    mock_llm_findings = [
        Finding(
            file="app/auth.py",
            line=2,
            severity="critical",
            category="security",
            title="SQL Injection in login query",
            comment="Interpolating variable into SQL query creates a severe vulnerability.",
            confidence=0.98,
        )
    ]

    body = json.dumps(SAMPLE_PR_PAYLOAD).encode("utf-8")
    sig = _compute_signature(secret, body)

    with (
        patch("app.main.fetch_pr_files", new_callable=AsyncMock) as mock_fetch,
        patch("app.agent.nodes.analyzer.ChatGroq") as mock_chat_groq,
        patch("app.agent.nodes.poster.post_review_comment", new_callable=AsyncMock) as mock_post_review,
        patch("app.agent.nodes.poster.post_summary_comment", new_callable=AsyncMock) as mock_post_summary,
    ):
        mock_fetch.return_value = mock_diff_files

        mock_llm_instance = mock_chat_groq.return_value
        mock_structured = AsyncMock()
        mock_structured.ainvoke.return_value = AnalyzerOutput(findings=mock_llm_findings)
        mock_llm_instance.with_structured_output.return_value = mock_structured

        mock_post_review.return_value = {"id": 1001}
        mock_post_summary.return_value = {"id": 2002}

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post(
                "/webhook/github",
                content=body,
                headers={
                    "X-Hub-Signature-256": sig,
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "delivery-e2e-1",
                },
            )

        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "accepted"
        assert "review_key" in data

        mock_fetch.assert_awaited_once_with("octocat/Hello-World", 7)
        mock_post_review.assert_awaited_once()
        mock_post_summary.assert_awaited_once()

        # Idempotency store recorded completed status
        review_key = "octocat/Hello-World#7@6dcb09b5b57875f334f61aebed695e2e4193db5e"
        assert await idempotency_store.get_status(review_key) == "completed"


@pytest.mark.asyncio
async def test_end_to_end_graph_direct_invocation(monkeypatch):
    """
    Direct invocation of compiled review_graph with mocked LLM and GitHub poster.
    Confirms state flow: retriever -> analyzer -> aggregator -> poster.
    """
    monkeypatch.setattr(settings, "GROQ_API_KEY", "mock-groq-key")

    initial_state = {
        "repo_owner": "org",
        "repo_name": "repo",
        "repo_full_name": "org/repo",
        "pr_number": 99,
        "pr_title": "Clean PR with no bugs",
        "pr_url": "https://github.com/org/repo/pull/99",
        "commit_sha": "def456",
        "changed_files": [
            {
                "filename": "README.md",
                "status": "modified",
                "patch": "@@ -1,2 +1,3 @@\n+New documentation line\n",
            }
        ],
        "retrieved_context": [],
        "raw_findings": [],
        "aggregated_findings": [],
        "final_summary": "",
        "final_verdict": "comment",
        "inline_comments_posted": 0,
    }

    with (
        patch("app.agent.nodes.analyzer.ChatGroq") as mock_chat_groq,
        patch("app.agent.nodes.poster.post_review_comment", new_callable=AsyncMock) as mock_post_review,
        patch("app.agent.nodes.poster.post_summary_comment", new_callable=AsyncMock) as mock_post_summary,
    ):
        mock_instance = mock_chat_groq.return_value
        mock_structured = AsyncMock()
        mock_structured.ainvoke.return_value = AnalyzerOutput(findings=[])
        mock_instance.with_structured_output.return_value = mock_structured

        mock_post_summary.return_value = {"id": 1}

        result = await review_graph.ainvoke(initial_state)

        assert result["final_verdict"] == "approve"
        assert result["aggregated_findings"] == []
        assert "Approved" in result["final_summary"]
        mock_post_review.assert_not_called()
        mock_post_summary.assert_awaited_once()


@pytest.mark.asyncio
async def test_webhook_fails_when_secret_not_configured(monkeypatch):
    """Webhook safely rejects requests with 500 when GITHUB_WEBHOOK_SECRET is empty."""
    monkeypatch.setattr(settings, "GITHUB_WEBHOOK_SECRET", "")
    body = json.dumps(SAMPLE_PR_PAYLOAD).encode("utf-8")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.post(
            "/webhook/github",
            content=body,
            headers={
                "X-Hub-Signature-256": "sha256=fake",
                "X-GitHub-Event": "pull_request",
            },
        )
    assert response.status_code == 500
    assert "Webhook secret not configured" in response.json()["detail"]


@pytest.mark.asyncio
async def test_webhook_rejects_pr_number_mismatch(monkeypatch):
    """Webhook rejects payload with 400 when top-level number != pull_request.number."""
    secret = "test_secret_123"
    monkeypatch.setattr(settings, "GITHUB_WEBHOOK_SECRET", secret)

    mismatched_payload = dict(SAMPLE_PR_PAYLOAD)
    mismatched_payload["number"] = 999  # Does not match pull_request.number (7)
    body = json.dumps(mismatched_payload).encode("utf-8")
    sig = _compute_signature(secret, body)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.post(
            "/webhook/github",
            content=body,
            headers={
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "pull_request",
            },
        )
    assert response.status_code == 400
    assert "PR number mismatch" in response.json()["detail"]


@pytest.mark.asyncio
async def test_webhook_handles_github_fetch_http_error(monkeypatch):
    """Webhook catches HTTP errors in background task and marks idempotency status as failed."""
    secret = "test_secret_123"
    monkeypatch.setattr(settings, "GITHUB_WEBHOOK_SECRET", secret)

    req = httpx.Request("GET", "https://api.github.com")
    resp = httpx.Response(status_code=404, request=req)

    body = json.dumps(SAMPLE_PR_PAYLOAD).encode("utf-8")
    sig = _compute_signature(secret, body)

    with patch("app.main.fetch_pr_files", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.side_effect = httpx.HTTPStatusError("Not Found", request=req, response=resp)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post(
                "/webhook/github",
                content=body,
                headers={
                    "X-Hub-Signature-256": sig,
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "fetch-http-err",
                },
            )

        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "accepted"

        # Background task failed and updated status to 'failed' for recovery
        review_key = "octocat/Hello-World#7@6dcb09b5b57875f334f61aebed695e2e4193db5e"
        assert await idempotency_store.get_status(review_key) == "failed"


@pytest.mark.asyncio
async def test_webhook_handles_github_fetch_generic_error(monkeypatch):
    """Webhook catches network/connection exceptions in background task and marks status as failed."""
    secret = "test_secret_123"
    monkeypatch.setattr(settings, "GITHUB_WEBHOOK_SECRET", secret)

    body = json.dumps(SAMPLE_PR_PAYLOAD).encode("utf-8")
    sig = _compute_signature(secret, body)

    with patch("app.main.fetch_pr_files", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.side_effect = httpx.ConnectTimeout("Connection timed out")

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post(
                "/webhook/github",
                content=body,
                headers={
                    "X-Hub-Signature-256": sig,
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "fetch-conn-err",
                },
            )

        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "accepted"

        review_key = "octocat/Hello-World#7@6dcb09b5b57875f334f61aebed695e2e4193db5e"
        assert await idempotency_store.get_status(review_key) == "failed"


# =========================================================================
# Phase 5A Webhook Async Processing & Idempotency Tests
# =========================================================================

@pytest.mark.asyncio
async def test_webhook_duplicate_delivery_id_ignored(monkeypatch):
    """Subsequent delivery with the same X-GitHub-Delivery is rejected with status 200 ignored."""
    secret = "test_secret_123"
    monkeypatch.setattr(settings, "GITHUB_WEBHOOK_SECRET", secret)

    body = json.dumps(SAMPLE_PR_PAYLOAD).encode("utf-8")
    sig = _compute_signature(secret, body)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        with patch("app.main.fetch_pr_files", new_callable=AsyncMock):
            # First delivery
            resp1 = await ac.post(
                "/webhook/github",
                content=body,
                headers={
                    "X-Hub-Signature-256": sig,
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "unique-delivery-guid-1",
                },
            )
            assert resp1.status_code == 202
            assert resp1.json()["status"] == "accepted"

            # Redelivery of same delivery ID
            resp2 = await ac.post(
                "/webhook/github",
                content=body,
                headers={
                    "X-Hub-Signature-256": sig,
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "unique-delivery-guid-1",
                },
            )
            assert resp2.status_code == 200
            data2 = resp2.json()
            assert data2["status"] == "ignored"
            assert "already received" in data2["reason"]


@pytest.mark.asyncio
async def test_webhook_same_pr_and_sha_different_delivery_ignored(monkeypatch):
    """Different delivery ID representing the same PR head commit is ignored."""
    secret = "test_secret_123"
    monkeypatch.setattr(settings, "GITHUB_WEBHOOK_SECRET", secret)

    body = json.dumps(SAMPLE_PR_PAYLOAD).encode("utf-8")
    sig = _compute_signature(secret, body)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        with patch("app.main.fetch_pr_files", new_callable=AsyncMock):
            # First delivery
            resp1 = await ac.post(
                "/webhook/github",
                content=body,
                headers={
                    "X-Hub-Signature-256": sig,
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "delivery-first",
                },
            )
            assert resp1.status_code == 202

            # Second delivery with different delivery ID but identical commit SHA
            resp2 = await ac.post(
                "/webhook/github",
                content=body,
                headers={
                    "X-Hub-Signature-256": sig,
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "delivery-second",
                },
            )
            assert resp2.status_code == 200
            assert resp2.json()["status"] == "ignored"
            assert "already" in resp2.json()["reason"]


@pytest.mark.asyncio
async def test_webhook_different_head_sha_both_reviewed(monkeypatch):
    """New commits (different head SHAs) on the same PR are both accepted for review."""
    secret = "test_secret_123"
    monkeypatch.setattr(settings, "GITHUB_WEBHOOK_SECRET", secret)

    payload_commit1 = dict(SAMPLE_PR_PAYLOAD)
    payload_commit1["pull_request"] = dict(SAMPLE_PR_PAYLOAD["pull_request"])
    payload_commit1["pull_request"]["head"] = {"sha": "sha_commit_1111"}

    payload_commit2 = dict(SAMPLE_PR_PAYLOAD)
    payload_commit2["pull_request"] = dict(SAMPLE_PR_PAYLOAD["pull_request"])
    payload_commit2["pull_request"]["head"] = {"sha": "sha_commit_2222"}

    body1 = json.dumps(payload_commit1).encode("utf-8")
    body2 = json.dumps(payload_commit2).encode("utf-8")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        with patch("app.main.fetch_pr_files", new_callable=AsyncMock):
            resp1 = await ac.post(
                "/webhook/github",
                content=body1,
                headers={
                    "X-Hub-Signature-256": _compute_signature(secret, body1),
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "deliv-sha1",
                },
            )
            resp2 = await ac.post(
                "/webhook/github",
                content=body2,
                headers={
                    "X-Hub-Signature-256": _compute_signature(secret, body2),
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "deliv-sha2",
                },
            )

    assert resp1.status_code == 202
    assert resp2.status_code == 202


@pytest.mark.asyncio
async def test_webhook_different_pr_both_reviewed(monkeypatch):
    """Different PR numbers on the same repository are both accepted."""
    secret = "test_secret_123"
    monkeypatch.setattr(settings, "GITHUB_WEBHOOK_SECRET", secret)

    payload_pr1 = dict(SAMPLE_PR_PAYLOAD)
    payload_pr1["number"] = 101
    payload_pr1["pull_request"] = dict(SAMPLE_PR_PAYLOAD["pull_request"])
    payload_pr1["pull_request"]["number"] = 101

    payload_pr2 = dict(SAMPLE_PR_PAYLOAD)
    payload_pr2["number"] = 102
    payload_pr2["pull_request"] = dict(SAMPLE_PR_PAYLOAD["pull_request"])
    payload_pr2["pull_request"]["number"] = 102

    body1 = json.dumps(payload_pr1).encode("utf-8")
    body2 = json.dumps(payload_pr2).encode("utf-8")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        with patch("app.main.fetch_pr_files", new_callable=AsyncMock):
            resp1 = await ac.post(
                "/webhook/github",
                content=body1,
                headers={
                    "X-Hub-Signature-256": _compute_signature(secret, body1),
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "deliv-pr101",
                },
            )
            resp2 = await ac.post(
                "/webhook/github",
                content=body2,
                headers={
                    "X-Hub-Signature-256": _compute_signature(secret, body2),
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "deliv-pr102",
                },
            )

    assert resp1.status_code == 202
    assert resp2.status_code == 202


@pytest.mark.asyncio
async def test_webhook_different_repo_both_reviewed(monkeypatch):
    """Different repositories are both accepted."""
    secret = "test_secret_123"
    monkeypatch.setattr(settings, "GITHUB_WEBHOOK_SECRET", secret)

    payload_repo1 = dict(SAMPLE_PR_PAYLOAD)
    payload_repo1["repository"] = {"full_name": "org/repo-one"}

    payload_repo2 = dict(SAMPLE_PR_PAYLOAD)
    payload_repo2["repository"] = {"full_name": "org/repo-two"}

    body1 = json.dumps(payload_repo1).encode("utf-8")
    body2 = json.dumps(payload_repo2).encode("utf-8")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        with patch("app.main.fetch_pr_files", new_callable=AsyncMock):
            resp1 = await ac.post(
                "/webhook/github",
                content=body1,
                headers={
                    "X-Hub-Signature-256": _compute_signature(secret, body1),
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "deliv-repo1",
                },
            )
            resp2 = await ac.post(
                "/webhook/github",
                content=body2,
                headers={
                    "X-Hub-Signature-256": _compute_signature(secret, body2),
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "deliv-repo2",
                },
            )

    assert resp1.status_code == 202
    assert resp2.status_code == 202


@pytest.mark.asyncio
async def test_webhook_concurrent_duplicate_requests(monkeypatch):
    """Concurrent requests for the same delivery or review key result in exactly one accepted."""
    import asyncio
    secret = "test_secret_123"
    monkeypatch.setattr(settings, "GITHUB_WEBHOOK_SECRET", secret)

    body = json.dumps(SAMPLE_PR_PAYLOAD).encode("utf-8")
    sig = _compute_signature(secret, body)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        with patch("app.main.fetch_pr_files", new_callable=AsyncMock):
            async def send_req(d_id: str):
                return await ac.post(
                    "/webhook/github",
                    content=body,
                    headers={
                        "X-Hub-Signature-256": sig,
                        "X-GitHub-Event": "pull_request",
                        "X-GitHub-Delivery": d_id,
                    },
                )

            # Fire 2 concurrent requests
            responses = await asyncio.gather(send_req("concurrent-1"), send_req("concurrent-2"))

    status_codes = [r.status_code for r in responses]
    assert 202 in status_codes
    assert 200 in status_codes
    assert status_codes.count(202) == 1
    assert status_codes.count(200) == 1


@pytest.mark.asyncio
async def test_webhook_background_failure_recoverable(monkeypatch):
    """When background review fails, idempotency status becomes 'failed' and allows subsequent retries."""
    secret = "test_secret_123"
    monkeypatch.setattr(settings, "GITHUB_WEBHOOK_SECRET", secret)

    body = json.dumps(SAMPLE_PR_PAYLOAD).encode("utf-8")
    sig = _compute_signature(secret, body)

    with (
        patch("app.main.fetch_pr_files", new_callable=AsyncMock) as mock_fetch,
        patch("app.worker.review_graph.ainvoke", new_callable=AsyncMock) as mock_graph,
    ):
        mock_fetch.return_value = [{"filename": "app/a.py", "patch": "@@ -1 +1 @@"}]
        mock_graph.side_effect = RuntimeError("Simulated crash in review_graph")

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            # First attempt fails in background
            resp1 = await ac.post(
                "/webhook/github",
                content=body,
                headers={
                    "X-Hub-Signature-256": sig,
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "fail-deliv-1",
                },
            )
            assert resp1.status_code == 202

            review_key = "octocat/Hello-World#7@6dcb09b5b57875f334f61aebed695e2e4193db5e"
            assert await idempotency_store.get_status(review_key) == "failed"

            # Subsequent attempt for same commit is accepted because failed state is recoverable
            mock_graph.side_effect = None
            mock_graph.return_value = {"final_verdict": "approve"}

            resp2 = await ac.post(
                "/webhook/github",
                content=body,
                headers={
                    "X-Hub-Signature-256": sig,
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "retry-deliv-2",
                },
            )
            assert resp2.status_code == 202
            assert await idempotency_store.get_status(review_key) == "completed"


@pytest.mark.asyncio
async def test_webhook_delivery_and_review_failure_retry_lifecycle(monkeypatch):
    """
    Verifies exact required sequence:
    1. request D1 + SHA1 -> accepted (202)
    2. while D1 + SHA1 is still in_progress -> D2 + SHA1 -> ignored (200)
    3. background review fails -> review key marked 'failed', D1 remains remembered
    4. request D1 again -> ignored (200) because D1 delivery ID was already seen
    5. request D2 + SHA1 -> accepted (202) as retry
    """
    import asyncio
    secret = "test_secret_123"
    monkeypatch.setattr(settings, "GITHUB_WEBHOOK_SECRET", secret)

    body = json.dumps(SAMPLE_PR_PAYLOAD).encode("utf-8")
    sig = _compute_signature(secret, body)
    review_key = "octocat/Hello-World#7@6dcb09b5b57875f334f61aebed695e2e4193db5e"

    # Pre-claim D1 to simulate an active in-progress state
    await idempotency_store.claim(delivery_id="D1", review_key=review_key)
    assert await idempotency_store.get_status(review_key) == "in_progress"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Step 2: D2 + SHA1 while D1 + SHA1 is in_progress -> ignored (200)
        resp_in_progress = await ac.post(
            "/webhook/github",
            content=body,
            headers={
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "D2",
            },
        )
        assert resp_in_progress.status_code == 200
        assert resp_in_progress.json()["status"] == "ignored"
        assert "already in progress" in resp_in_progress.json()["reason"]

        # Step 3: background review for D1 fails
        await idempotency_store.mark_failed(review_key)
        assert await idempotency_store.get_status(review_key) == "failed"

        # Step 4: D1 sent again -> ignored (200) because D1 was already received
        resp_d1_again = await ac.post(
            "/webhook/github",
            content=body,
            headers={
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "D1",
            },
        )
        assert resp_d1_again.status_code == 200
        assert resp_d1_again.json()["status"] == "ignored"
        assert "already received" in resp_d1_again.json()["reason"]

        # Step 5: D2 (new delivery) + SHA1 -> accepted (202) as retry
        with (
            patch("app.main.fetch_pr_files", new_callable=AsyncMock) as mock_fetch,
            patch("app.worker.review_graph.ainvoke", new_callable=AsyncMock) as mock_graph,
        ):
            mock_fetch.return_value = [{"filename": "app/a.py", "patch": "@@ -1 +1 @@"}]
            mock_graph.return_value = {"final_verdict": "approve"}

            resp_d2_retry = await ac.post(
                "/webhook/github",
                content=body,
                headers={
                    "X-Hub-Signature-256": sig,
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "D2",
                },
            )
            assert resp_d2_retry.status_code == 202
            assert resp_d2_retry.json()["status"] == "accepted"
            assert await idempotency_store.get_status(review_key) == "completed"



