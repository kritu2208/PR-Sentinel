"""
Tests for app/github_client.py.
Verifies pagination, timeout configuration, and comment posting.
No real network calls are made.
"""
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
import pytest

from app.config import settings
from app.github_client import (
    _headers,
    _timeout,
    fetch_pr_files,
    post_review_comment,
    post_summary_comment,
)


def test_headers_includes_token(monkeypatch):
    """Headers include Bearer token when configured."""
    monkeypatch.setattr(settings, "GITHUB_TOKEN", "test-pat-token")
    headers = _headers()
    assert headers["Authorization"] == "Bearer test-pat-token"
    assert headers["Accept"] == "application/vnd.github+json"


def test_headers_omits_authorization_when_empty(monkeypatch):
    """Headers safely omit Authorization header when GITHUB_TOKEN is empty."""
    monkeypatch.setattr(settings, "GITHUB_TOKEN", "")
    headers = _headers()
    assert "Authorization" not in headers


def test_timeout_configuration(monkeypatch):
    """_timeout returns httpx.Timeout with configured settings."""
    monkeypatch.setattr(settings, "GITHUB_REQUEST_TIMEOUT", 25.0)
    t = _timeout()
    assert isinstance(t, httpx.Timeout)
    assert t.connect == 25.0
    assert t.read == 25.0


@pytest.mark.asyncio
async def test_fetch_pr_files_pagination_multiple_pages():
    """
    Verifies that fetch_pr_files paginates across multiple pages:
    - Page 1 returns 100 items (full page).
    - Page 2 returns 20 items (< 100, so last page).
    - All 120 items are combined into the result.
    """
    page_1_data = [{"filename": f"file_{i}.py", "patch": "@@ -1 +1 @@"} for i in range(100)]
    page_2_data = [{"filename": f"file_{100 + i}.py", "patch": "@@ -1 +1 @@"} for i in range(20)]

    mock_client = AsyncMock()
    mock_resp_1 = MagicMock()
    mock_resp_1.json.return_value = page_1_data
    mock_resp_1.raise_for_status.return_value = None

    mock_resp_2 = MagicMock()
    mock_resp_2.json.return_value = page_2_data
    mock_resp_2.raise_for_status.return_value = None

    mock_client.get.side_effect = [mock_resp_1, mock_resp_2]

    with patch("httpx.AsyncClient") as mock_async_client_cls:
        mock_async_client_cls.return_value.__aenter__.return_value = mock_client

        files = await fetch_pr_files("octocat/Hello-World", 42)

        assert len(files) == 120
        assert mock_client.get.call_count == 2

        # Verify query parameters for each page
        call_1_params = mock_client.get.call_args_list[0].kwargs["params"]
        assert call_1_params == {"page": 1, "per_page": 100}

        call_2_params = mock_client.get.call_args_list[1].kwargs["params"]
        assert call_2_params == {"page": 2, "per_page": 100}


@pytest.mark.asyncio
async def test_fetch_pr_files_pagination_stops_on_empty_page():
    """Pagination terminates immediately when first page is empty."""
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.json.return_value = []
    mock_resp.raise_for_status.return_value = None
    mock_client.get.return_value = mock_resp

    with patch("httpx.AsyncClient") as mock_async_client_cls:
        mock_async_client_cls.return_value.__aenter__.return_value = mock_client

        files = await fetch_pr_files("octocat/Hello-World", 42)

        assert files == []
        assert mock_client.get.call_count == 1


@pytest.mark.asyncio
async def test_github_client_uses_timeout_in_all_calls():
    """Verifies that httpx.AsyncClient is instantiated with the configured timeout in all functions."""
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.json.return_value = []
    mock_resp.raise_for_status.return_value = None
    mock_client.get.return_value = mock_resp
    mock_client.post.return_value = mock_resp

    with patch("httpx.AsyncClient") as mock_async_client_cls:
        mock_async_client_cls.return_value.__aenter__.return_value = mock_client

        # 1. fetch_pr_files
        await fetch_pr_files("owner/repo", 1)
        assert "timeout" in mock_async_client_cls.call_args.kwargs
        assert isinstance(mock_async_client_cls.call_args.kwargs["timeout"], httpx.Timeout)

        # 2. post_review_comment
        await post_review_comment("owner/repo", 1, "sha", "app/main.py", 10, "Comment")
        assert "timeout" in mock_async_client_cls.call_args.kwargs

        # 3. post_summary_comment
        await post_summary_comment("owner/repo", 1, "Summary")
        assert "timeout" in mock_async_client_cls.call_args.kwargs
