"""
Tests for the Analyzer node.
All LLM calls are mocked - no real network calls are made.
"""
from unittest.mock import AsyncMock, patch
import pytest

from app.agent.nodes.analyzer import analyzer_node
from app.agent.schemas import AnalyzerOutput, Finding
from app.config import settings


@pytest.mark.asyncio
async def test_analyzer_structured_output_parsing(monkeypatch):
    """Analyzer parses structured output into raw findings correctly."""
    monkeypatch.setattr(settings, "GROQ_API_KEY", "mock-groq-key")

    mock_findings = [
        Finding(
            file="app/main.py",
            line=12,
            severity="high",
            category="security",
            title="Hardcoded credential risk",
            comment="Avoid embedding credentials directly.",
            confidence=0.95,
        ),
        Finding(
            file="app/utils.py",
            line=45,
            severity="medium",
            category="bug",
            title="Possible ZeroDivisionError",
            comment="Check divisor before division.",
            confidence=0.88,
        ),
    ]
    mock_output = AnalyzerOutput(findings=mock_findings)

    with patch("app.agent.nodes.analyzer.ChatGroq") as mock_chat_groq:
        mock_instance = mock_chat_groq.return_value
        mock_structured = AsyncMock()
        mock_structured.ainvoke.return_value = mock_output
        mock_instance.with_structured_output.return_value = mock_structured

        state = {
            "pr_title": "Fix auth logic",
            "changed_files": [
                {
                    "filename": "app/main.py",
                    "status": "modified",
                    "patch": "@@ -10,3 +10,4 @@\n+SECRET = '123'\n",
                }
            ],
            "retrieved_context": [],
        }

        result = await analyzer_node(state)
        assert "raw_findings" in result
        findings = result["raw_findings"]
        assert len(findings) == 2
        assert findings[0]["file"] == "app/main.py"
        assert findings[0]["severity"] == "high"
        assert findings[0]["category"] == "security"
        assert findings[1]["file"] == "app/utils.py"


@pytest.mark.asyncio
async def test_analyzer_empty_findings_when_clean(monkeypatch):
    """Analyzer handles empty findings when LLM detects no issues."""
    monkeypatch.setattr(settings, "GROQ_API_KEY", "mock-groq-key")

    with patch("app.agent.nodes.analyzer.ChatGroq") as mock_chat_groq:
        mock_instance = mock_chat_groq.return_value
        mock_structured = AsyncMock()
        mock_structured.ainvoke.return_value = AnalyzerOutput(findings=[])
        mock_instance.with_structured_output.return_value = mock_structured

        state = {
            "pr_title": "Minor doc update",
            "changed_files": [
                {
                    "filename": "docs/readme.txt",
                    "status": "modified",
                    "patch": "@@ -1,1 +1,1 @@\n-Hello\n+Hello World\n",
                }
            ],
            "retrieved_context": [],
        }

        result = await analyzer_node(state)
        assert result["raw_findings"] == []


@pytest.mark.asyncio
async def test_analyzer_skips_unreviewable_files():
    """Analyzer skips LLM invocation completely when no reviewable files exist."""
    state = {
        "pr_title": "Update locks",
        "changed_files": [
            {"filename": "package-lock.json", "patch": "@@ -1,2 +1,2 @@\n"},
            {"filename": "logo.png", "patch": None},
        ],
    }
    result = await analyzer_node(state)
    assert result["raw_findings"] == []
    assert result["reviewed_files"] == []


@pytest.mark.asyncio
async def test_analyzer_missing_api_key(monkeypatch):
    """Analyzer safely handles missing GROQ_API_KEY without crashing."""
    monkeypatch.setattr(settings, "GROQ_API_KEY", "")

    state = {
        "pr_title": "Test PR",
        "changed_files": [{"filename": "app/main.py", "patch": "@@ -1,2 +1,2 @@\n+code"}],
    }

    result = await analyzer_node(state)
    assert result["raw_findings"] == []
    assert "error" in result
    assert "GROQ_API_KEY" in result["error"]


@pytest.mark.asyncio
async def test_analyzer_handles_llm_exception(monkeypatch):
    """Analyzer safely catches LLM exceptions and does not leak secrets."""
    monkeypatch.setattr(settings, "GROQ_API_KEY", "super-secret-key")

    with patch("app.agent.nodes.analyzer.ChatGroq") as mock_chat_groq:
        mock_instance = mock_chat_groq.return_value
        mock_structured = AsyncMock()
        mock_structured.ainvoke.side_effect = RuntimeError("Groq rate limit exceeded")
        mock_instance.with_structured_output.return_value = mock_structured

        state = {
            "pr_title": "Test PR",
            "changed_files": [{"filename": "app/main.py", "patch": "@@ -1,2 +1,2 @@\n+code"}],
        }

        result = await analyzer_node(state)
        assert result["raw_findings"] == []
        assert "error" in result
        assert "super-secret-key" not in result["error"]
