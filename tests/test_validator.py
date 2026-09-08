"""
Tests for the Validator node.
Verifies rejection of hallucinated files, invalid diff lines, malformed schemas,
and preservation of valid findings.
"""
import pytest
from app.agent.schemas import Finding
from app.agent.validator import validator_node

SAMPLE_PATCH = """@@ -10,4 +10,6 @@ def example():
     x = 1
-    y = 2
+    y = 20
+    z = 30
     return x + y
"""


@pytest.mark.asyncio
async def test_validator_accepts_valid_diff_line():
    """Valid finding matching a changed file and valid line in diff hunk is accepted."""
    state = {
        "changed_files": [
            {"filename": "app/main.py", "patch": SAMPLE_PATCH},
        ],
        "raw_findings": [
            {
                "file": "app/main.py",
                "line": 11,  # In hunk (new line 11)
                "severity": "high",
                "category": "bug",
                "title": "Variable reassignment issue",
                "comment": "y is overwritten unexpectedly.",
                "confidence": 0.9,
            }
        ],
    }

    result = await validator_node(state)
    assert len(result["validated_findings"]) == 1
    assert result["validated_findings"][0]["file"] == "app/main.py"
    assert result["validated_findings"][0]["line"] == 11
    assert result["validated_findings"][0]["title"] == "Variable reassignment issue"


@pytest.mark.asyncio
async def test_validator_accepts_file_level_finding_when_line_is_none():
    """File-level finding with line=None is accepted as long as file exists in changed_files."""
    state = {
        "changed_files": [
            {"filename": "app/main.py", "patch": SAMPLE_PATCH},
        ],
        "raw_findings": [
            {
                "file": "app/main.py",
                "line": None,
                "severity": "medium",
                "category": "quality",
                "title": "Module too large",
                "comment": "Consider refactoring into smaller modules.",
                "confidence": 0.8,
            }
        ],
    }

    result = await validator_node(state)
    assert len(result["validated_findings"]) == 1
    assert result["validated_findings"][0]["line"] is None


@pytest.mark.asyncio
async def test_validator_rejects_hallucinated_file():
    """Findings on files that do not exist in changed_files are rejected."""
    state = {
        "changed_files": [
            {"filename": "app/main.py", "patch": SAMPLE_PATCH},
        ],
        "raw_findings": [
            {
                "file": "app/non_existent.py",  # Hallucinated file
                "line": 10,
                "severity": "critical",
                "category": "security",
                "title": "Secret leaked",
                "comment": "Secret found in file.",
                "confidence": 0.99,
            }
        ],
    }

    result = await validator_node(state)
    assert len(result["validated_findings"]) == 0


@pytest.mark.asyncio
async def test_validator_rejects_out_of_hunk_line():
    """Findings referencing lines outside the diff hunk are rejected."""
    state = {
        "changed_files": [
            {"filename": "app/main.py", "patch": SAMPLE_PATCH},
        ],
        "raw_findings": [
            {
                "file": "app/main.py",
                "line": 999,  # Outside hunk
                "severity": "low",
                "category": "quality",
                "title": "Old line issue",
                "comment": "Issue on line outside patch.",
                "confidence": 0.85,
            }
        ],
    }

    result = await validator_node(state)
    assert len(result["validated_findings"]) == 0


@pytest.mark.asyncio
async def test_validator_rejects_malformed_schema():
    """Malformed findings failing Pydantic schema validation are safely rejected without crashing."""
    state = {
        "changed_files": [
            {"filename": "app/main.py", "patch": SAMPLE_PATCH},
        ],
        "raw_findings": [
            {
                "file": "app/main.py",
                "line": 11,
                "severity": "super-urgent-not-valid",  # Invalid severity
                "category": "bug",
                "title": "Invalid finding",
                "comment": "Has invalid severity.",
                "confidence": 0.9,
            },
            "totally-not-a-dict",  # Not a dict or Finding
            {
                # Missing required fields like title, comment, etc.
                "file": "app/main.py",
            },
        ],
    }

    result = await validator_node(state)
    assert result["validated_findings"] == []


@pytest.mark.asyncio
async def test_validator_handles_empty_raw_findings():
    """Validator safely returns empty validated_findings when raw_findings is empty or None."""
    result_empty = await validator_node({"changed_files": [], "raw_findings": []})
    assert result_empty == {"validated_findings": []}

    result_none = await validator_node({})
    assert result_none == {"validated_findings": []}


@pytest.mark.asyncio
async def test_validator_accepts_pydantic_finding_objects():
    """Validator handles Finding instances directly and preserves them."""
    finding = Finding(
        file="app/main.py",
        line=11,
        severity="high",
        category="security",
        title="Valid Finding Object",
        comment="Direct Pydantic object test.",
        confidence=0.95,
    )
    state = {
        "changed_files": [{"filename": "app/main.py", "patch": SAMPLE_PATCH}],
        "raw_findings": [finding],
    }

    result = await validator_node(state)
    assert len(result["validated_findings"]) == 1
    assert result["validated_findings"][0]["title"] == "Valid Finding Object"


@pytest.mark.asyncio
async def test_validator_pipeline_integration(monkeypatch):
    """
    End-to-end pipeline verification:
    Ensures hallucinated files and out-of-hunk lines produced by Analyzer are
    filtered out by Validator and never reach Aggregator or Poster.
    """
    from unittest.mock import AsyncMock, patch
    from app.agent.graph import review_graph
    from app.agent.schemas import AnalyzerOutput
    from app.config import settings

    monkeypatch.setattr(settings, "GROQ_API_KEY", "mock-key")

    mock_llm_findings = [
        # 1. Valid finding
        Finding(
            file="app/main.py",
            line=11,
            severity="critical",
            category="bug",
            title="Real Bug",
            comment="Valid issue in diff.",
            confidence=0.99,
        ),
        # 2. Hallucinated file
        Finding(
            file="app/ghost_file.py",
            line=5,
            severity="critical",
            category="security",
            title="Hallucinated file finding",
            comment="This file does not exist.",
            confidence=0.99,
        ),
        # 3. Line outside diff hunk
        Finding(
            file="app/main.py",
            line=999,
            severity="high",
            category="security",
            title="Hallucinated line finding",
            comment="This line is not in the diff.",
            confidence=0.99,
        ),
    ]

    initial_state = {
        "repo_owner": "owner",
        "repo_name": "repo",
        "repo_full_name": "owner/repo",
        "pr_number": 1,
        "pr_title": "PR with hallucinated findings",
        "pr_url": "https://github.com/owner/repo/pull/1",
        "commit_sha": "abc1234",
        "changed_files": [{"filename": "app/main.py", "patch": SAMPLE_PATCH}],
        "retrieved_context": [],
        "raw_findings": [],
        "validated_findings": [],
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
        mock_structured.ainvoke.return_value = AnalyzerOutput(findings=mock_llm_findings)
        mock_instance.with_structured_output.return_value = mock_structured

        mock_post_review.return_value = {"id": 1}
        mock_post_summary.return_value = {"id": 2}

        result = await review_graph.ainvoke(initial_state)

        # Raw findings contains all 3 from analyzer
        assert len(result["raw_findings"]) == 3

        # Validator filtered out hallucinated file and out-of-hunk line!
        assert len(result["validated_findings"]) == 1
        assert result["validated_findings"][0]["title"] == "Real Bug"
        assert result["validated_findings"][0]["file"] == "app/main.py"

        # Aggregator received only validated finding
        assert len(result["aggregated_findings"]) == 1
        assert result["aggregated_findings"][0]["title"] == "Real Bug"

        # Poster only posted inline comment for the 1 valid finding
        assert result["inline_comments_posted"] == 1
        mock_post_review.assert_awaited_once()
        assert mock_post_review.await_args.kwargs["file_path"] == "app/main.py"
        assert mock_post_review.await_args.kwargs["line"] == 11


@pytest.mark.asyncio
async def test_validator_accepts_reviewed_file():
    """Finding for an explicitly reviewed file is accepted."""
    state = {
        "changed_files": [{"filename": "app/main.py", "patch": SAMPLE_PATCH}],
        "reviewed_files": ["app/main.py"],
        "raw_findings": [
            {
                "file": "app/main.py",
                "line": 11,
                "severity": "high",
                "category": "bug",
                "title": "Bug in reviewed file",
                "comment": "Valid finding.",
                "confidence": 0.9,
            }
        ],
    }
    result = await validator_node(state)
    assert len(result["validated_findings"]) == 1
    assert result["validated_findings"][0]["file"] == "app/main.py"


@pytest.mark.asyncio
async def test_validator_rejects_unreviewed_changed_file():
    """Finding for a file present in changed_files but NOT in reviewed_files is rejected."""
    state = {
        "changed_files": [
            {"filename": "app/reviewed.py", "patch": SAMPLE_PATCH},
            {"filename": "app/unreviewed.py", "patch": SAMPLE_PATCH},
        ],
        "reviewed_files": ["app/reviewed.py"],  # LLM was only given reviewed.py
        "raw_findings": [
            {
                "file": "app/unreviewed.py",  # In changed_files, but NOT reviewed by LLM
                "line": 11,
                "severity": "high",
                "category": "bug",
                "title": "Finding on unreviewed file",
                "comment": "Should be rejected.",
                "confidence": 0.9,
            }
        ],
    }
    result = await validator_node(state)
    assert len(result["validated_findings"]) == 0


@pytest.mark.asyncio
async def test_validator_rejects_all_when_reviewed_files_empty():
    """When reviewed_files is empty, no findings are accepted."""
    state = {
        "changed_files": [{"filename": "app/main.py", "patch": SAMPLE_PATCH}],
        "reviewed_files": [],  # Empty reviewed files
        "raw_findings": [
            {
                "file": "app/main.py",
                "line": 11,
                "severity": "high",
                "category": "bug",
                "title": "Bug in main",
                "comment": "Should be rejected because reviewed_files is empty.",
                "confidence": 0.9,
            }
        ],
    }
    result = await validator_node(state)
    assert len(result["validated_findings"]) == 0


@pytest.mark.asyncio
async def test_analyzer_records_exact_reviewed_files_honoring_limit(monkeypatch):
    """Analyzer records exactly the filenames whose patches are sent to the LLM, respecting MAX_FILES_TO_REVIEW."""
    from unittest.mock import AsyncMock, patch
    from app.agent.nodes.analyzer import analyzer_node
    from app.agent.schemas import AnalyzerOutput
    from app.config import settings

    monkeypatch.setattr(settings, "GROQ_API_KEY", "mock-key")
    monkeypatch.setattr(settings, "MAX_FILES_TO_REVIEW", 2)

    # 3 reviewable files + 1 unreviewable
    state = {
        "pr_title": "Multi-file PR",
        "changed_files": [
            {"filename": "app/a.py", "patch": "@@ -1 +1 @@\n+a"},
            {"filename": "app/b.py", "patch": "@@ -1 +1 @@\n+b"},
            {"filename": "app/c.py", "patch": "@@ -1 +1 @@\n+c"},
            {"filename": "package-lock.json", "patch": "@@ -1 +1 @@\n+lock"},
        ],
    }

    with patch("app.agent.nodes.analyzer.ChatGroq") as mock_chat_groq:
        mock_instance = mock_chat_groq.return_value
        mock_structured = AsyncMock()
        mock_structured.ainvoke.return_value = AnalyzerOutput(findings=[])
        mock_instance.with_structured_output.return_value = mock_structured

        result = await analyzer_node(state)
        # Limit was 2, so only a.py and b.py should be reviewed
        assert result["reviewed_files"] == ["app/a.py", "app/b.py"]


