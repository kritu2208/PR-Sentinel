"""
Tests for the Codebase-Aware Retriever Node:
- Empty patch yields empty context
- Active retrieval fetches and slices cross-file symbol definitions
- Graceful degradation on network/unexpected errors
"""
from unittest.mock import patch
import pytest
from app.agent.nodes.retriever import retriever_node


@pytest.mark.asyncio
async def test_retriever_returns_empty_context():
    """Retriever returns empty retrieved_context list when patch contains no imports or symbols."""
    state = {
        "repo_full_name": "octocat/Hello-World",
        "pr_number": 42,
        "changed_files": [{"filename": "app/main.py", "patch": "@@ -1,3 +1,4 @@"}],
    }
    result = await retriever_node(state)
    assert "retrieved_context" in result
    assert result["retrieved_context"] == []


@pytest.mark.asyncio
async def test_retriever_active_cross_file_context():
    """Retriever node extracts symbol, fetches file, and populates retrieved_context."""
    async def mock_fetch(repo: str, path: str, ref: str | None) -> str | None:
        if path == "app/models/invoice.py":
            return (
                "from pydantic import BaseModel\n\n"
                "class Invoice(BaseModel):\n"
                "    id: int\n"
                "    total: float\n"
            )
        return None

    state = {
        "repo_full_name": "octocat/Hello-World",
        "commit_sha": "sha_pr_42",
        "pr_number": 42,
        "changed_files": [
            {
                "filename": "app/services/checkout.py",
                "patch": (
                    "@@ -1,3 +1,6 @@\n"
                    "+from app.models.invoice import Invoice\n"
                    "+def checkout(inv: Invoice):\n"
                    "+    pass\n"
                ),
            }
        ],
    }

    with patch("app.agent.nodes.retriever.fetch_file_content", side_effect=mock_fetch):
        result = await retriever_node(state)
        assert "retrieved_context" in result
        context = result["retrieved_context"]
        assert len(context) == 1
        assert context[0]["path"] == "app/models/invoice.py"
        assert "Invoice" in context[0]["symbol"]
        assert "class Invoice(BaseModel):" in context[0]["content"]


@pytest.mark.asyncio
async def test_retriever_graceful_exception_handling():
    """Retriever node safely catches unexpected fetch exceptions and returns empty context."""
    state = {
        "repo_full_name": "octocat/Hello-World",
        "commit_sha": "sha_pr_42",
        "pr_number": 42,
        "changed_files": [
            {
                "filename": "app/services/checkout.py",
                "patch": "@@ -1,2 +1,3 @@\n+from app.models import Invoice\n",
            }
        ],
    }

    with patch("app.agent.nodes.retriever.fetch_file_content", side_effect=RuntimeError("GitHub API timeout")):
        result = await retriever_node(state)
        assert result == {"retrieved_context": []}
