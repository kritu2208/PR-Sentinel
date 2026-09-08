"""
Tests for the Aggregator node.
Verifies deduplication, severity ordering, verdict calculation, and summary creation.
"""
import pytest
from app.agent.nodes.aggregator import (
    aggregator_node,
    deduplicate_and_merge_findings,
    determine_verdict,
)


def test_deduplicate_and_merge_duplicate_lines():
    """Overlapping findings on the same line are merged and higher severity is preserved."""
    raw = [
        {
            "file": "app/main.py",
            "line": 20,
            "severity": "medium",
            "category": "bug",
            "title": "SQL Injection vulnerability",
            "comment": "Concatenating user input into query.",
            "confidence": 0.85,
        },
        {
            "file": "app/main.py",
            "line": 20,
            "severity": "critical",
            "category": "security",
            "title": "SQL Injection vulnerability",
            "comment": "Unsanitized parameter passed to db.execute.",
            "confidence": 0.95,
        },
    ]

    deduped = deduplicate_and_merge_findings(raw, min_confidence=0.7)
    assert len(deduped) == 1
    # Critical should prevail over medium
    assert deduped[0]["severity"] == "critical"
    assert deduped[0]["confidence"] == 0.95


def test_deduplicate_filters_low_confidence():
    """Findings below the confidence threshold are discarded."""
    raw = [
        {
            "file": "app/main.py",
            "line": 10,
            "severity": "low",
            "category": "quality",
            "title": "Unsure about variable name",
            "comment": "Could be named better.",
            "confidence": 0.50,
        },
        {
            "file": "app/main.py",
            "line": 15,
            "severity": "high",
            "category": "bug",
            "title": "Confirmed NPE",
            "comment": "Object is None when user is anonymous.",
            "confidence": 0.90,
        },
    ]

    filtered = deduplicate_and_merge_findings(raw, min_confidence=0.7)
    assert len(filtered) == 1
    assert filtered[0]["title"] == "Confirmed NPE"


def test_severity_ordering():
    """Findings are sorted: critical -> high -> medium -> low."""
    raw = [
        {"file": "a.py", "line": 1, "severity": "low", "title": "L", "confidence": 0.9},
        {"file": "b.py", "line": 2, "severity": "critical", "title": "C", "confidence": 0.9},
        {"file": "c.py", "line": 3, "severity": "medium", "title": "M", "confidence": 0.9},
        {"file": "d.py", "line": 4, "severity": "high", "title": "H", "confidence": 0.9},
    ]

    sorted_findings = deduplicate_and_merge_findings(raw, min_confidence=0.5)
    severities = [f["severity"] for f in sorted_findings]
    assert severities == ["critical", "high", "medium", "low"]


def test_determine_verdict():
    """Verifies verdict logic: critical/high -> request_changes, medium/low -> comment, empty -> approve."""
    # Critical issue -> request_changes
    assert determine_verdict([{"severity": "critical"}]) == "request_changes"
    assert determine_verdict([{"severity": "high"}]) == "request_changes"

    # Medium or low only -> comment
    assert determine_verdict([{"severity": "medium"}]) == "comment"
    assert determine_verdict([{"severity": "low"}]) == "comment"

    # Empty list -> approve
    assert determine_verdict([]) == "approve"

    # Error present -> comment
    assert determine_verdict([], error="LLM failed") == "comment"


@pytest.mark.asyncio
async def test_aggregator_node_execution():
    """Aggregator node integration test with state."""
    state = {
        "validated_findings": [
            {
                "file": "app/api.py",
                "line": 30,
                "severity": "high",
                "category": "security",
                "title": "Missing Auth check",
                "comment": "Endpoint exposes private user data.",
                "confidence": 0.95,
            }
        ],
        "changed_files": [{"filename": "app/api.py", "patch": "@@ -1,5 +1,5 @@"}],
    }

    result = await aggregator_node(state)
    assert result["final_verdict"] == "request_changes"
    assert len(result["aggregated_findings"]) == 1
    assert "Missing Auth check" in result["final_summary"]
    assert "request_changes" in result["final_verdict"]


def test_deduplicate_nearby_lines_different_issues_not_merged():
    """Nearby lines with different titles/issues must NOT be merged."""
    raw = [
        {
            "file": "app/payment.py",
            "line": 100,
            "severity": "high",
            "category": "bug",
            "title": "Missing authentication check",
            "comment": "Endpoint does not verify JWT token.",
            "confidence": 0.95,
        },
        {
            "file": "app/payment.py",
            "line": 102,
            "severity": "high",
            "category": "bug",
            "title": "Incorrect transaction amount",
            "comment": "Amount is calculated without checking currency scale.",
            "confidence": 0.95,
        },
    ]

    deduped = deduplicate_and_merge_findings(raw, min_confidence=0.7)
    assert len(deduped) == 2
    titles = {f["title"] for f in deduped}
    assert "Missing authentication check" in titles
    assert "Incorrect transaction amount" in titles


def test_deduplicate_nearby_lines_same_issue_merged():
    """Nearby lines with strongly similar titles and matching category are merged."""
    raw = [
        {
            "file": "app/auth.py",
            "line": 40,
            "severity": "high",
            "category": "security",
            "title": "SQL injection in login query",
            "comment": "User input passed to format string.",
            "confidence": 0.90,
        },
        {
            "file": "app/auth.py",
            "line": 41,
            "severity": "critical",
            "category": "security",
            "title": "SQL injection via login parameter",
            "comment": "Vulnerable query execution.",
            "confidence": 0.98,
        },
    ]

    deduped = deduplicate_and_merge_findings(raw, min_confidence=0.7)
    assert len(deduped) == 1
    assert deduped[0]["severity"] == "critical"
    assert deduped[0]["confidence"] == 0.98


def test_deduplicate_same_title_nearby_lines_merged():
    """Identical normalized title on the same file with same category within proximity is treated as duplicate reports and merged."""
    raw = [
        {
            "file": "app/config.py",
            "line": 15,
            "severity": "medium",
            "category": "security",
            "title": "Hardcoded API secret token",
            "comment": "Found secret on line 15.",
            "confidence": 0.85,
        },
        {
            "file": "app/config.py",
            "line": 17,
            "severity": "high",
            "category": "security",
            "title": "Hardcoded API secret token",
            "comment": "Found secret on line 17.",
            "confidence": 0.95,
        },
    ]

    deduped = deduplicate_and_merge_findings(raw, min_confidence=0.7)
    assert len(deduped) == 1
    assert deduped[0]["severity"] == "high"
    assert deduped[0]["line"] == 17


def test_deduplicate_severity_promotion_retains_line():
    """When a duplicate finding with higher severity merges, it adopts the incoming finding's line number."""
    raw = [
        {
            "file": "app.py",
            "line": None,
            "severity": "low",
            "category": "security",
            "title": "Hardcoded credential",
            "comment": "Low severity file-level notice.",
            "confidence": 0.80,
        },
        {
            "file": "app.py",
            "line": 55,
            "severity": "critical",
            "category": "security",
            "title": "Hardcoded credential",
            "comment": "Critical credential at line 55.",
            "confidence": 0.98,
        },
    ]

    deduped = deduplicate_and_merge_findings(raw, min_confidence=0.7)
    assert len(deduped) == 1
    assert deduped[0]["severity"] == "critical"
    assert deduped[0]["line"] == 55


def test_deduplicate_valid_line_not_lost_when_incoming_has_line_none():
    """A valid existing line is not lost when merged with a duplicate that has line=None."""
    # Existing valid line with incoming lower severity
    raw_lower = [
        {
            "file": "app.py",
            "line": 55,
            "severity": "critical",
            "category": "security",
            "title": "Hardcoded credential",
            "comment": "Found critical credential on line 55.",
            "confidence": 0.95,
        },
        {
            "file": "app.py",
            "line": None,
            "severity": "low",
            "category": "security",
            "title": "Hardcoded credential",
            "comment": "General file-level notice.",
            "confidence": 0.85,
        },
    ]
    deduped_lower = deduplicate_and_merge_findings(raw_lower, min_confidence=0.7)
    assert len(deduped_lower) == 1
    assert deduped_lower[0]["severity"] == "critical"
    assert deduped_lower[0]["line"] == 55

    # Existing valid line with incoming equal severity
    raw_equal = [
        {
            "file": "app.py",
            "line": 55,
            "severity": "high",
            "category": "security",
            "title": "Hardcoded credential",
            "comment": "Found credential on line 55.",
            "confidence": 0.90,
        },
        {
            "file": "app.py",
            "line": None,
            "severity": "high",
            "category": "security",
            "title": "Hardcoded credential",
            "comment": "Duplicate finding with line None.",
            "confidence": 0.90,
        },
    ]
    deduped_equal = deduplicate_and_merge_findings(raw_equal, min_confidence=0.7)
    assert len(deduped_equal) == 1
    assert deduped_equal[0]["line"] == 55


def test_deduplicate_same_title_distant_lines_not_merged():
    """Identical title and same category on distant lines must NOT be merged."""
    raw = [
        {
            "file": "payment.py",
            "line": 10,
            "severity": "high",
            "category": "bug",
            "title": "Missing null check",
            "comment": "Object user can be None.",
            "confidence": 0.95,
        },
        {
            "file": "payment.py",
            "line": 300,
            "severity": "high",
            "category": "bug",
            "title": "Missing null check",
            "comment": "Object config can be None.",
            "confidence": 0.95,
        },
    ]

    deduped = deduplicate_and_merge_findings(raw, min_confidence=0.7)
    assert len(deduped) == 2
    lines = {f["line"] for f in deduped}
    assert lines == {10, 300}


def test_determine_verdict_critical_or_high_with_error_precedence():
    """Critical and high findings always produce 'request_changes', even when errors occurred."""
    # Critical finding + error => request_changes
    assert determine_verdict([{"severity": "critical"}], error="retriever failed") == "request_changes"

    # High finding + error => request_changes
    assert determine_verdict([{"severity": "high"}], error="some warning") == "request_changes"

    # No findings + error => comment
    assert determine_verdict([], error="retriever failed") == "comment"

    # No findings + no error => approve
    assert determine_verdict([]) == "approve"


def test_deduplicate_different_files_never_merged():
    """Findings on different files are never merged regardless of title similarity."""
    raw = [
        {
            "file": "app/service_a.py",
            "line": 10,
            "severity": "high",
            "category": "bug",
            "title": "Uncaught ValueError exception",
            "comment": "int() cast can throw.",
            "confidence": 0.9,
        },
        {
            "file": "app/service_b.py",
            "line": 10,
            "severity": "high",
            "category": "bug",
            "title": "Uncaught ValueError exception",
            "comment": "int() cast can throw.",
            "confidence": 0.9,
        },
    ]

    deduped = deduplicate_and_merge_findings(raw, min_confidence=0.7)
    assert len(deduped) == 2


def test_deduplicate_different_categories_similar_wording_not_merged():
    """Different categories with similar wording are treated as distinct concerns and not merged."""
    raw = [
        {
            "file": "app/stream.py",
            "line": 50,
            "severity": "high",
            "category": "security",
            "title": "Unbounded memory consumption vulnerability",
            "comment": "DoS risk from unauthenticated client.",
            "confidence": 0.9,
        },
        {
            "file": "app/stream.py",
            "line": 51,
            "severity": "medium",
            "category": "performance",
            "title": "Excessive memory consumption in stream reader",
            "comment": "Large chunk size creates high memory pressure.",
            "confidence": 0.85,
        },
    ]

    deduped = deduplicate_and_merge_findings(raw, min_confidence=0.7)
    assert len(deduped) == 2

