"""
Tests for the Poster node.
Verifies inline comment posting, summary posting, line safety, and GitHub API error handling.
"""
from unittest.mock import AsyncMock, patch
import httpx
import pytest

from app.agent.nodes.poster import poster_node


@pytest.mark.asyncio
async def test_poster_posts_inline_and_summary():
    """Poster posts inline comments for valid diff lines and posts summary comment."""
    state = {
        "repo_full_name": "owner/repo",
        "pr_number": 10,
        "commit_sha": "abc1234567890",
        "changed_files": [
            {
                "filename": "app/main.py",
                "patch": "@@ -1,5 +1,6 @@\n line1\n+line2_added\n line3\n",
            }
        ],
        "aggregated_findings": [
            {
                "file": "app/main.py",
                "line": 2,  # valid line in diff hunk
                "severity": "high",
                "category": "security",
                "title": "Vulnerability Found",
                "comment": "Fix this immediately.",
                "confidence": 0.95,
            }
        ],
        "final_summary": "## Summary: 1 High Issue",
    }

    with (
        patch("app.agent.nodes.poster.post_review_comment", new_callable=AsyncMock) as mock_review_comment,
        patch("app.agent.nodes.poster.post_summary_comment", new_callable=AsyncMock) as mock_summary_comment,
    ):
        mock_review_comment.return_value = {"id": 101}
        mock_summary_comment.return_value = {"id": 202}

        result = await poster_node(state)

        assert result["inline_comments_posted"] == 1
        mock_review_comment.assert_awaited_once()
        kall = mock_review_comment.await_args.kwargs
        assert kall["repo_full_name"] == "owner/repo"
        assert kall["pr_number"] == 10
        assert kall["commit_sha"] == "abc1234567890"
        assert kall["file_path"] == "app/main.py"
        assert kall["line"] == 2
        assert "Vulnerability Found" in kall["body"]

        mock_summary_comment.assert_awaited_once_with(
            repo_full_name="owner/repo",
            pr_number=10,
            body="## Summary: 1 High Issue",
        )


@pytest.mark.asyncio
async def test_poster_defers_unmapped_lines_to_summary():
    """Findings with invalid or missing line numbers are appended to summary rather than called as inline comments."""
    state = {
        "repo_full_name": "owner/repo",
        "pr_number": 10,
        "commit_sha": "abc123",
        "changed_files": [
            {
                "filename": "app/main.py",
                "patch": "@@ -1,2 +1,2 @@\n-old\n+new\n",
            }
        ],
        "aggregated_findings": [
            {
                "file": "app/main.py",
                "line": 999,  # outside hunk
                "severity": "medium",
                "category": "quality",
                "title": "General structure issue",
                "comment": "Module is too large.",
                "confidence": 0.85,
            }
        ],
        "final_summary": "## Initial Summary",
    }

    with (
        patch("app.agent.nodes.poster.post_review_comment", new_callable=AsyncMock) as mock_review_comment,
        patch("app.agent.nodes.poster.post_summary_comment", new_callable=AsyncMock) as mock_summary_comment,
    ):
        result = await poster_node(state)

        # Inline comment should NOT be called for line 999
        mock_review_comment.assert_not_called()
        assert result["inline_comments_posted"] == 0

        # Summary comment should be called with unmapped finding included
        mock_summary_comment.assert_awaited_once()
        summary_arg = mock_summary_comment.await_args.kwargs["body"]
        assert "General structure issue" in summary_arg
        assert "Additional File-Level & Context Findings" in summary_arg


@pytest.mark.asyncio
async def test_poster_handles_github_422_fallback():
    """If GitHub rejects an inline comment (e.g. 422), the poster gracefully diverts it to summary."""
    state = {
        "repo_full_name": "owner/repo",
        "pr_number": 10,
        "commit_sha": "abc123",
        "changed_files": [
            {
                "filename": "app/main.py",
                "patch": "@@ -1,3 +1,3 @@\n+line1\n",
            }
        ],
        "aggregated_findings": [
            {
                "file": "app/main.py",
                "line": 1,
                "severity": "critical",
                "category": "bug",
                "title": "Crash on startup",
                "comment": "Missing import.",
                "confidence": 0.99,
            },
            {
                "file": "app/main.py",
                "line": 1,
                "severity": "low",
                "category": "quality",
                "title": "Docstring missing",
                "comment": "Add docstring.",
                "confidence": 0.8,
            }
        ],
        "final_summary": "## Initial Summary",
    }

    # Simulate GitHub returning 422 for one of the inline calls
    req = httpx.Request("POST", "https://api.github.com")
    resp = httpx.Response(status_code=422, request=req)

    with (
        patch("app.agent.nodes.poster.post_review_comment", new_callable=AsyncMock) as mock_review_comment,
        patch("app.agent.nodes.poster.post_summary_comment", new_callable=AsyncMock) as mock_summary_comment,
    ):
        mock_review_comment.side_effect = [
            {"id": 1},  # First succeeds
            httpx.HTTPStatusError("Unprocessable Entity", request=req, response=resp),  # Second fails
        ]

        result = await poster_node(state)
        assert result["inline_comments_posted"] == 1

        mock_summary_comment.assert_awaited_once()
        summary_arg = mock_summary_comment.await_args.kwargs["body"]
        # The failed one should be in the summary
        assert "Docstring missing" in summary_arg
