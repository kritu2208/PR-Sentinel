"""
Aggregator Node for PR Sentinel.
Deduplicates findings, prioritizes by severity, filters noise, determines verdict, and builds PR summary.
"""
import logging
from app.agent.schemas import Finding, SeverityLevel, VerdictType
from app.agent.state import ReviewState
from app.config import settings

logger = logging.getLogger("pr-sentinel.aggregator")

SEVERITY_ORDER: dict[SeverityLevel, int] = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}


import re

STOP_WORDS = {"in", "on", "at", "to", "for", "of", "with", "a", "an", "the", "and", "or", "is", "via", "from", "by"}


def _normalize_title(title: str) -> str:
    """Normalizes titles for exact equality checks."""
    return "".join(c.lower() for c in title if c.isalnum())


def _tokenize(text: str) -> set[str]:
    """Tokenizes text into content words, removing common stop words."""
    words = re.findall(r"\w+", text.lower())
    return {w for w in words if w not in STOP_WORDS}


def _token_similarity(s1: set[str], s2: set[str]) -> float:
    """Calculates Jaccard similarity across token sets."""
    if not s1 or not s2:
        return 0.0
    intersection = len(s1 & s2)
    union = len(s1 | s2)
    return intersection / union if union > 0 else 0.0


MAX_SAME_TITLE_LINE_DISTANCE: int = 3


def _are_findings_duplicates(f1: dict, f2: dict) -> bool:
    """
    Determines if two findings represent the same underlying defect:
    1. Must target the exact same file.
    2. Proximity alone is NOT sufficient.
    3. Exact same normalized title on the same file with same category merges only if nearby
       (within MAX_SAME_TITLE_LINE_DISTANCE) or at least one is file-level (line=None).
    4. Nearby lines (within 2 lines) require strong title token similarity (>= 0.5) and matching category.
    5. Different categories on the exact same line merge (model miscategorized same defect).
    6. Different categories with non-identical titles or distant lines are treated as distinct issues.
    """
    if f1.get("file") != f2.get("file"):
        return False

    cat1 = f1.get("category")
    cat2 = f2.get("category")
    line1 = f1.get("line")
    line2 = f2.get("line")

    t1_norm = _normalize_title(f1.get("title", ""))
    t2_norm = _normalize_title(f2.get("title", ""))

    # Exact same normalized title on the same file
    if t1_norm and t1_norm == t2_norm:
        if cat1 == cat2:
            # If both have lines, merge only when within close proximity
            if line1 is not None and line2 is not None:
                return abs(line1 - line2) <= MAX_SAME_TITLE_LINE_DISTANCE
            # If at least one is a file-level finding (line=None), merge them
            return True
        # Different categories on the exact same line: model miscategorized same defect
        if line1 is not None and line1 == line2:
            return True
        return False

    # Different categories with non-identical titles are not merged
    if cat1 != cat2:
        return False

    # Same category: nearby lines require strong semantic title similarity
    lines_nearby = (
        line1 is not None and line2 is not None and abs(line1 - line2) <= 2
    )
    if lines_nearby:
        tokens1 = _tokenize(f1.get("title", ""))
        tokens2 = _tokenize(f2.get("title", ""))
        sim = _token_similarity(tokens1, tokens2)
        if sim >= 0.5:
            return True

    return False


def deduplicate_and_merge_findings(raw_findings: list[dict], min_confidence: float) -> list[dict]:
    """
    Deduplicates and merges overlapping findings:
    1. Filters out findings with confidence below min_confidence.
    2. Groups findings targeting the same file with strong semantic identity.
    3. Keeps highest severity / confidence and merges unique comments.
    4. Retains the best line number (never overwrites valid line with None, promotes line on higher severity).
    """
    valid_findings = []
    for item in raw_findings:
        conf = float(item.get("confidence", 1.0))
        if conf < min_confidence:
            logger.debug(f"Discarding low-confidence finding ({conf} < {min_confidence}): {item.get('title')}")
            continue
        valid_findings.append(item)

    merged: list[dict] = []

    for item in valid_findings:
        f_sev = item.get("severity", "medium")

        is_duplicate = False
        for existing in merged:
            if _are_findings_duplicates(item, existing):
                is_duplicate = True
                e_sev = existing.get("severity", "medium")
                e_rank = SEVERITY_ORDER.get(e_sev, 2)
                f_rank = SEVERITY_ORDER.get(f_sev, 2)

                # If current finding is higher severity, promote it
                if f_rank < e_rank:
                    existing["severity"] = f_sev
                    existing["title"] = item.get("title", existing["title"])
                    if item.get("category"):
                        existing["category"] = item.get("category")
                    # Adopt line from higher-severity finding if non-null
                    if item.get("line") is not None:
                        existing["line"] = item.get("line")
                    if item.get("root_cause"):
                        existing["root_cause"] = item.get("root_cause")
                    if item.get("evidence"):
                        existing["evidence"] = item.get("evidence")
                    if item.get("impact"):
                        existing["impact"] = item.get("impact")
                    if item.get("recommendation"):
                        existing["recommendation"] = item.get("recommendation")
                    if item.get("investigation_status"):
                        existing["investigation_status"] = item.get("investigation_status")
                else:
                    if not existing.get("root_cause") and item.get("root_cause"):
                        existing["root_cause"] = item.get("root_cause")
                    if not existing.get("evidence") and item.get("evidence"):
                        existing["evidence"] = item.get("evidence")
                    if not existing.get("impact") and item.get("impact"):
                        existing["impact"] = item.get("impact")
                    if not existing.get("recommendation") and item.get("recommendation"):
                        existing["recommendation"] = item.get("recommendation")
                    if not existing.get("investigation_status") and item.get("investigation_status"):
                        existing["investigation_status"] = item.get("investigation_status")
                if existing.get("line") is None and item.get("line") is not None:
                    # Retain valid line from incoming finding if existing line was None
                    existing["line"] = item.get("line")

                # Merge distinct comment details
                item_comment = item.get("comment", "").strip()
                if item_comment and item_comment not in existing.get("comment", ""):
                    existing["comment"] = f"{existing.get('comment', '')}\n\n*Note:* {item_comment}"

                # Retain max confidence
                existing["confidence"] = max(
                    float(existing.get("confidence", 1.0)),
                    float(item.get("confidence", 1.0)),
                )
                break

        if not is_duplicate:
            merged.append(dict(item))

    # Sort findings by severity (critical first) then confidence (highest first)
    merged.sort(
        key=lambda x: (
            SEVERITY_ORDER.get(x.get("severity", "medium"), 2),
            -float(x.get("confidence", 1.0)),
        )
    )

    return merged


def determine_verdict(
    findings: list[dict],
    error: str | None = None,
    total_files_count: int = 0,
) -> VerdictType:
    """
    Produces final review verdict based on findings and error status:
    1. Any critical or high -> 'request_changes' (never downgraded by error)
    2. Else if error occurred -> 'comment' (prevents approving incomplete review)
    3. Else if medium or low findings -> 'comment'
    4. Else -> 'approve'
    """
    has_critical_or_high = any(
        f.get("severity") in ("critical", "high") for f in findings
    )
    if has_critical_or_high:
        return "request_changes"

    if error:
        return "comment"

    if findings:
        return "comment"

    return "approve"


def build_summary(
    verdict: VerdictType,
    findings: list[dict],
    total_files_count: int,
    error: str | None = None,
) -> str:
    """
    Builds a structured markdown summary for the PR.
    """
    verdict_headers = {
        "approve": "## 🛡️ PR Sentinel: Approved ✅",
        "comment": "## 🛡️ PR Sentinel: Review Comments ⚠️",
        "request_changes": "## 🛡️ PR Sentinel: Changes Requested ❌",
    }

    header = verdict_headers.get(verdict, "## 🛡️ PR Sentinel: Review Complete")
    lines = [header, ""]

    if error:
        lines.append(f"> ⚠️ **Notice:** AI analysis encountered a limitation: `{error}`. Review was partially completed.\n")

    severity_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for f in findings:
        sev = f.get("severity", "medium")
        if sev in severity_counts:
            severity_counts[sev] += 1

    lines.append(
        f"Analyzed **{total_files_count}** changed file(s). "
        f"Found **{len(findings)}** actionable finding(s):"
    )
    lines.append(
        f"- 🔴 **Critical:** {severity_counts['critical']} | "
        f"🟠 **High:** {severity_counts['high']} | "
        f"🟡 **Medium:** {severity_counts['medium']} | "
        f"🟢 **Low:** {severity_counts['low']}\n"
    )

    if findings:
        lines.append("### Key Findings")
        for idx, f in enumerate(findings, 1):
            sev_badge = f"[{f.get('severity', 'info').upper()}]"
            loc = f"`{f.get('file', 'unknown')}`"
            if f.get("line"):
                loc += f":L{f.get('line')}"
            lines.append(f"{idx}. **{sev_badge}** {loc} — **{f.get('title', 'Issue')}**")
            lines.append(f"   > {f.get('comment', '').splitlines()[0]}")

    else:
        lines.append("✨ No bugs, security vulnerabilities, or quality concerns detected in the reviewed diffs.")

    lines.append("\n---\n*Powered by PR Sentinel AI Review Pipeline (LangGraph + Groq)*")
    return "\n".join(lines)


async def aggregator_node(state: ReviewState) -> dict:
    """
    Aggregator node: removes duplicate findings, sorts by severity, produces verdict and PR summary.
    """
    validated_findings = state.get("investigated_findings") or state.get("validated_findings") or []
    changed_files = state.get("changed_files") or []
    error = state.get("error")

    aggregated = deduplicate_and_merge_findings(
        validated_findings,
        min_confidence=settings.MIN_CONFIDENCE_THRESHOLD,
    )

    verdict = determine_verdict(
        aggregated,
        error=error,
        total_files_count=len(changed_files),
    )

    summary = build_summary(
        verdict=verdict,
        findings=aggregated,
        total_files_count=len(changed_files),
        error=error,
    )

    logger.info(
        f"Aggregator completed: {len(aggregated)} finding(s) retained, verdict='{verdict}'"
    )

    return {
        "aggregated_findings": aggregated,
        "final_verdict": verdict,
        "final_summary": summary,
    }
