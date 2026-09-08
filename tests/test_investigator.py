"""
Tests for the Autonomous Investigation / Root Cause Analysis Layer.
"""
from unittest.mock import AsyncMock, patch
import pytest

from app.agent.graph import build_review_graph, review_graph
from app.agent.nodes.aggregator import aggregator_node
from app.agent.nodes.investigator import investigator_node
from app.agent.prompts import INVESTIGATOR_SYSTEM_PROMPT, build_investigator_user_prompt
from app.agent.schemas import Finding, InvestigationItem, InvestigatorOutput
from app.config import settings


@pytest.mark.asyncio
async def test_investigator_receives_only_validated_findings(monkeypatch):
    """Investigator processes validated findings and skips when validated_findings is empty."""
    monkeypatch.setattr(settings, "GROQ_API_KEY", "mock-groq-key")

    state = {
        "pr_title": "Fix security vulnerability",
        "changed_files": [{"filename": "app/auth.py", "patch": "@@ -1,3 +1,4 @@\n+token = 'bad'"}],
        "validated_findings": [],
        "retrieved_context": [],
    }

    result = await investigator_node(state)
    assert result["investigated_findings"] == []
    assert result["validated_findings"] == []


@pytest.mark.asyncio
async def test_investigator_populates_root_cause_and_evidence(monkeypatch):
    """Investigator correctly populates root cause, evidence, impact, and recommendation."""
    monkeypatch.setattr(settings, "GROQ_API_KEY", "mock-groq-key")

    mock_investigation = InvestigationItem(
        file="app/auth.py",
        line=15,
        title="Unverified JWT signature",
        root_cause="decode() is called with verify_signature=False, allowing token forgery.",
        evidence="diff line 15: jwt.decode(token, verify=False)",
        impact="Attackers can forge arbitrary administrative tokens and escalate privileges.",
        recommendation="Enable strict JWT cryptographic verification with the server public key.",
        confidence=0.98,
        status="confirmed",
    )
    mock_output = InvestigatorOutput(investigations=[mock_investigation])

    with patch("app.agent.nodes.investigator.ChatGroq") as mock_chat_groq:
        mock_instance = mock_chat_groq.return_value
        mock_structured = AsyncMock()
        mock_structured.ainvoke.return_value = mock_output
        mock_instance.with_structured_output.return_value = mock_structured

        state = {
            "pr_title": "Auth enhancement",
            "changed_files": [
                {
                    "filename": "app/auth.py",
                    "patch": "@@ -10,6 +10,7 @@\n+token = jwt.decode(raw, verify=False)\n",
                }
            ],
            "validated_findings": [
                {
                    "file": "app/auth.py",
                    "line": 15,
                    "severity": "critical",
                    "category": "security",
                    "title": "Unverified JWT signature",
                    "comment": "JWT signature verification is disabled.",
                    "confidence": 0.95,
                }
            ],
            "retrieved_context": [
                {
                    "path": "app/config.py",
                    "symbol": "JWT_SECRET",
                    "content": "JWT_SECRET = 'sample'",
                }
            ],
        }

        result = await investigator_node(state)
        investigated = result["investigated_findings"]
        assert len(investigated) == 1
        f = investigated[0]
        assert f["root_cause"] == "decode() is called with verify_signature=False, allowing token forgery."
        assert "jwt.decode" in f["evidence"]
        assert "Attackers can forge" in f["impact"]
        assert "Enable strict JWT" in f["recommendation"]
        assert f["investigation_status"] == "confirmed"
        assert f["confidence"] == 0.95


@pytest.mark.asyncio
async def test_investigator_handles_insufficient_evidence_without_fabrication(monkeypatch):
    """When evidence is insufficient, investigator marks status as uncertain rather than fabricating."""
    monkeypatch.setattr(settings, "GROQ_API_KEY", "mock-groq-key")

    mock_investigation = InvestigationItem(
        file="app/services.py",
        line=40,
        title="Potential race condition",
        root_cause="Shared state access without lock.",
        evidence="Insufficient context to conclusively verify lock mechanism across processes.",
        impact="May cause inconsistent balance if called concurrently.",
        recommendation="Add distributed mutex lock or database row-level locking.",
        confidence=0.55,
        status="uncertain",
    )
    mock_output = InvestigatorOutput(investigations=[mock_investigation])

    with patch("app.agent.nodes.investigator.ChatGroq") as mock_chat_groq:
        mock_instance = mock_chat_groq.return_value
        mock_structured = AsyncMock()
        mock_structured.ainvoke.return_value = mock_output
        mock_instance.with_structured_output.return_value = mock_structured

        state = {
            "pr_title": "Service logic update",
            "changed_files": [{"filename": "app/services.py", "patch": "@@ -38,3 +38,4 @@\n+balance += amount\n"}],
            "validated_findings": [
                {
                    "file": "app/services.py",
                    "line": 40,
                    "severity": "medium",
                    "category": "bug",
                    "title": "Potential race condition",
                    "comment": "Check concurrency safety.",
                    "confidence": 0.85,
                }
            ],
            "retrieved_context": [],
        }

        result = await investigator_node(state)
        f = result["investigated_findings"][0]
        assert f["investigation_status"] == "uncertain"
        assert "Insufficient context" in f["evidence"]
        # Confidence scaled down when uncertain
        assert f["confidence"] <= 0.85


@pytest.mark.asyncio
async def test_investigator_failure_degrades_gracefully_without_corrupting_review(monkeypatch):
    """LLM network or API exceptions during investigation do not crash the review."""
    monkeypatch.setattr(settings, "GROQ_API_KEY", "mock-groq-key")

    with patch("app.agent.nodes.investigator.ChatGroq") as mock_chat_groq:
        mock_instance = mock_chat_groq.return_value
        mock_structured = AsyncMock()
        mock_structured.ainvoke.side_effect = RuntimeError("Groq model timeout during investigation")
        mock_instance.with_structured_output.return_value = mock_structured

        state = {
            "pr_title": "PR with finding",
            "changed_files": [{"filename": "app/main.py", "patch": "@@ -1,2 +1,2 @@\n+code"}],
            "validated_findings": [
                {
                    "file": "app/main.py",
                    "line": 1,
                    "severity": "high",
                    "category": "bug",
                    "title": "Null pointer risk",
                    "comment": "Variable may be None.",
                    "confidence": 0.9,
                }
            ],
            "retrieved_context": [],
        }

        result = await investigator_node(state)
        assert len(result["investigated_findings"]) == 1
        f = result["investigated_findings"][0]
        assert f["file"] == "app/main.py"
        assert f["title"] == "Null pointer risk"
        assert f["investigation_status"] == "failed"


@pytest.mark.asyncio
async def test_investigator_skips_low_confidence_or_trivial_findings(monkeypatch):
    """Investigator skips LLM calls for low-severity or low-confidence findings."""
    monkeypatch.setattr(settings, "GROQ_API_KEY", "mock-groq-key")

    with patch("app.agent.nodes.investigator.ChatGroq") as mock_chat_groq:
        state = {
            "pr_title": "Style updates",
            "changed_files": [{"filename": "app/main.py", "patch": "@@ -1,2 +1,2 @@\n+pass"}],
            "validated_findings": [
                {
                    "file": "app/main.py",
                    "line": 1,
                    "severity": "low",
                    "category": "quality",
                    "title": "Minor formatting issue",
                    "comment": "Consider line break.",
                    "confidence": 0.65,
                }
            ],
            "retrieved_context": [],
        }

        result = await investigator_node(state)
        # ChatGroq was NOT called for trivial finding
        mock_chat_groq.assert_not_called()
        assert len(result["investigated_findings"]) == 1
        assert result["investigated_findings"][0]["investigation_status"] == "skipped"


def test_investigator_prompt_injection_and_secret_protection():
    """Validates that investigator system and user prompts enforce strict security boundaries."""
    assert "UNTRUSTED DATA" in INVESTIGATOR_SYSTEM_PROMPT
    assert "NEVER reproduce secrets" in INVESTIGATOR_SYSTEM_PROMPT
    assert "DO NOT invent" in INVESTIGATOR_SYSTEM_PROMPT

    user_prompt = build_investigator_user_prompt(
        pr_title="PR #1",
        findings=[{"file": "app.py", "line": 10, "title": "Secret leak", "comment": "Token in source"}],
        changed_files=[{"filename": "app.py", "patch": "+token = '123'"}],
        retrieved_context=[{"path": "config.py", "content": "KEY=val"}],
    )
    assert "UNTRUSTED RETRIEVED REPOSITORY CONTEXT" in user_prompt
    assert "UNTRUSTED CHANGED FILES & PATCHES" in user_prompt
    assert "VALIDATED FINDINGS TO INVESTIGATE" in user_prompt


@pytest.mark.asyncio
async def test_graph_flow_includes_investigator_and_preserves_aggregation():
    """Validates full review graph topology and end-to-end node integration."""
    compiled = build_review_graph()
    nodes = compiled.nodes
    assert "investigator" in nodes

    # Verify aggregator handles investigated findings seamlessly
    state = {
        "investigated_findings": [
            {
                "file": "app/auth.py",
                "line": 12,
                "severity": "critical",
                "category": "security",
                "title": "SQL Injection",
                "comment": "Unsanitized query input.",
                "confidence": 0.95,
                "root_cause": "Raw f-string SQL query concatenation.",
                "evidence": "cursor.execute(f'SELECT * WHERE id={user_id}')",
                "impact": "Full database exfiltration.",
                "recommendation": "Use parameterized queries: cursor.execute('SELECT * WHERE id=%s', (user_id,))",
                "investigation_status": "confirmed",
            }
        ],
        "changed_files": [{"filename": "app/auth.py"}],
    }

    res = await aggregator_node(state)
    assert res["final_verdict"] == "request_changes"
    agg = res["aggregated_findings"]
    assert len(agg) == 1
    assert agg[0]["root_cause"] == "Raw f-string SQL query concatenation."
    assert "SQL Injection" in res["final_summary"]
