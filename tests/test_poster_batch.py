"""
Tests for Phase 6 Atomic Pull Request Reviews (POST /repos/{owner}/{repo}/pulls/{number}/reviews).
Covers batch comment construction, verdict event mapping (APPROVE, REQUEST_CHANGES, COMMENT),
HTTP 422 diff-hunk rejection fallback, and legacy mock interception.
"""
from unittest.mock import AsyncMock, patch
import httpx
import pytest

from app.agent.nodes.poster import poster_node
from app.config import settings


@pytest.mark.asyncio
async def test_batch_review_submits_atomic_payload_with_approve():
    """Valid findings and 'approve' verdict are bundled into a single atomic review call."""
    state = {
        "repo_full_name": "owner/repo",
        "pr_number": 42,
        "commit_sha": "abc1234567890",
        "changed_files": [
            {
                "filename": "auth.py",
                "patch": "@@ -1,5 +1,6 @@\n line1\n+line2_added\n line3\n",
            }
        ],
        "aggregated_findings": [
            {
                "file": "auth.py",
                "line": 2,
                "severity": "low",
                "category": "style",
                "title": "Minor formatting",
                "comment": "Looks good overall.",
                "confidence": 0.9,
            }
        ],
        "final_summary": "## LGTM",
        "final_verdict": "approve",
    }

    with patch("app.agent.nodes.poster.create_pull_request_review", new_callable=AsyncMock) as mock_batch:
        mock_batch.return_value = {"id": 99991, "state": "APPROVED"}

        result = await poster_node(state)

        assert result["inline_comments_posted"] == 1
        assert result["final_verdict"] == "approve"
        assert result["github_review_id"] == "99991"

        mock_batch.assert_awaited_once()
        kall = mock_batch.await_args.kwargs
        assert kall["repo_full_name"] == "owner/repo"
        assert kall["pr_number"] == 42
        assert kall["commit_sha"] == "abc1234567890"
        assert kall["event"] == "APPROVE"
        assert kall["body"] == "## LGTM"
        assert len(kall["comments"]) == 1
        comment = kall["comments"][0]
        assert comment["path"] == "auth.py"
        assert comment["line"] == 2
        assert comment["side"] == "RIGHT"
        assert "Minor formatting" in comment["body"]


@pytest.mark.asyncio
async def test_batch_review_maps_request_changes_and_comment_events():
    """Correctly maps request_changes and comment verdicts to GitHub review events."""
    with patch("app.agent.nodes.poster.create_pull_request_review", new_callable=AsyncMock) as mock_batch:
        mock_batch.return_value = {"id": 99992}

        # Test request_changes
        state_rc = {
            "repo_full_name": "owner/repo",
            "pr_number": 43,
            "commit_sha": "sha_rc",
            "changed_files": [],
            "aggregated_findings": [],
            "final_summary": "Changes needed",
            "final_verdict": "request_changes",
        }
        await poster_node(state_rc)
        assert mock_batch.await_args.kwargs["event"] == "REQUEST_CHANGES"

        # Test comment (default/neutral)
        state_comm = {
            "repo_full_name": "owner/repo",
            "pr_number": 44,
            "commit_sha": "sha_comm",
            "changed_files": [],
            "aggregated_findings": [],
            "final_summary": "Questions",
            "final_verdict": "comment",
        }
        await poster_node(state_comm)
        assert mock_batch.await_args.kwargs["event"] == "COMMENT"


@pytest.mark.asyncio
async def test_batch_review_http_422_fallback_to_summary_review():
    """
    If GitHub rejects comments with HTTP 422 (e.g. line outside diff hunk),
    the poster falls back to submitting a summary-only review with all findings in the body.
    """
    state = {
        "repo_full_name": "owner/repo",
        "pr_number": 45,
        "commit_sha": "sha_422",
        "changed_files": [
            {
                "filename": "service.py",
                "patch": "@@ -10,3 +10,3 @@\n+valid_line\n",
            }
        ],
        "aggregated_findings": [
            {
                "file": "service.py",
                "line": 10,
                "severity": "high",
                "category": "bug",
                "title": "Uncaught exception",
                "comment": "Add try/except here.",
                "confidence": 0.95,
            }
        ],
        "final_summary": "## Summary Findings",
        "final_verdict": "request_changes",
    }

    req = httpx.Request("POST", "https://api.github.com")
    resp_422 = httpx.Response(status_code=422, request=req)

    with patch("app.agent.nodes.poster.create_pull_request_review", new_callable=AsyncMock) as mock_batch:
        # First call fails with 422, fallback call succeeds
        mock_batch.side_effect = [
            httpx.HTTPStatusError("Unprocessable Entity", request=req, response=resp_422),
            {"id": 88888},
        ]

        result = await poster_node(state)

        assert mock_batch.await_count == 2
        # First call included comments
        first_call = mock_batch.await_args_list[0].kwargs
        assert first_call["comments"] is not None

        # Second call submitted with comments=None and comments merged into body
        second_call = mock_batch.await_args_list[1].kwargs
        assert second_call["comments"] is None
        assert "Uncaught exception" in second_call["body"]
        assert result["github_review_id"] == "88888"
        assert result["inline_comments_posted"] == 0


@pytest.mark.asyncio
async def test_batch_review_unmapped_findings_appended_to_summary():
    """Findings whose lines are outside diff hunks are automatically added to summary body."""
    state = {
        "repo_full_name": "owner/repo",
        "pr_number": 46,
        "commit_sha": "sha_unmapped",
        "changed_files": [
            {
                "filename": "service.py",
                "patch": "@@ -1,3 +1,3 @@\n+line1\n",
            }
        ],
        "aggregated_findings": [
            {
                "file": "service.py",
                "line": 999,  # outside hunk
                "severity": "medium",
                "category": "architecture",
                "title": "File too long",
                "comment": "Split this module.",
                "confidence": 0.8,
            }
        ],
        "final_summary": "## Architecture Review",
        "final_verdict": "comment",
    }

    with patch("app.agent.nodes.poster.create_pull_request_review", new_callable=AsyncMock) as mock_batch:
        mock_batch.return_value = {"id": 77777}

        result = await poster_node(state)

        assert result["inline_comments_posted"] == 0
        mock_batch.assert_awaited_once()
        body = mock_batch.await_args.kwargs["body"]
        assert "File too long" in body
        assert "Split this module." in body
        assert mock_batch.await_args.kwargs["comments"] is None


@pytest.mark.asyncio
async def test_batch_review_caps_inline_comments_to_max_limit(monkeypatch):
    """When inline comments exceed MAX_INLINE_COMMENTS_PER_REVIEW, excess comments are appended to summary."""
    monkeypatch.setattr(settings, "MAX_INLINE_COMMENTS_PER_REVIEW", 3)

    # 5 findings on valid line 1
    findings = [
        {
            "file": "main.py",
            "line": 1,
            "severity": "high",
            "category": "bug",
            "title": f"Issue {i}",
            "comment": f"Comment {i}",
            "confidence": 0.9,
        }
        for i in range(1, 6)
    ]

    state = {
        "repo_full_name": "owner/repo",
        "pr_number": 47,
        "commit_sha": "sha_cap",
        "changed_files": [{"filename": "main.py", "patch": "@@ -1,1 +1,2 @@\n+line1\n"}],
        "aggregated_findings": findings,
        "final_summary": "## Code Health",
        "final_verdict": "request_changes",
    }

    with patch("app.agent.nodes.poster.create_pull_request_review", new_callable=AsyncMock) as mock_batch:
        mock_batch.return_value = {"id": 66666}
        result = await poster_node(state)

        assert result["inline_comments_posted"] == 3
        kall = mock_batch.await_args.kwargs
        assert len(kall["comments"]) == 3
        # Remaining 2 findings should be appended to body
        assert "Issue 4" in kall["body"]
        assert "Issue 5" in kall["body"]


@pytest.mark.asyncio
async def test_batch_review_posts_commit_status_when_enabled(monkeypatch):
    """When ENABLE_COMMIT_STATUS is True, create_commit_status is invoked with appropriate verdict status."""
    monkeypatch.setattr(settings, "ENABLE_COMMIT_STATUS", True)

    state = {
        "repo_full_name": "owner/repo",
        "pr_number": 48,
        "commit_sha": "sha_status",
        "changed_files": [],
        "aggregated_findings": [],
        "final_summary": "All good",
        "final_verdict": "approve",
    }

    with patch("app.agent.nodes.poster.create_pull_request_review", new_callable=AsyncMock) as mock_batch, \
         patch("app.agent.nodes.poster.create_commit_status", new_callable=AsyncMock) as mock_status:
        mock_batch.return_value = {"id": 55555}
        mock_status.return_value = {"id": 11111, "state": "success"}

        await poster_node(state)

        mock_status.assert_awaited_once()
        kall = mock_status.await_args.kwargs
        assert kall["repo_full_name"] == "owner/repo"
        assert kall["commit_sha"] == "sha_status"
        assert kall["state"] == "success"
        assert "Approved" in kall["description"]


@pytest.mark.asyncio
async def test_poster_aborts_on_transient_error():
    """Poster skips calling GitHub review API if pipeline encountered a transient error."""
    state = {
        "repo_full_name": "owner/repo",
        "pr_number": 49,
        "commit_sha": "sha_transient",
        "changed_files": [],
        "aggregated_findings": [],
        "final_summary": "Should not post",
        "final_verdict": "comment",
        "transient_error": True,
    }

    with patch("app.agent.nodes.poster.create_pull_request_review", new_callable=AsyncMock) as mock_batch:
        result = await poster_node(state)

        mock_batch.assert_not_called()
        assert result["error"] == "transient_error"
        assert result["inline_comments_posted"] == 0

