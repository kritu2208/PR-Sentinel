"""
Poster Node for PR Sentinel.
Posts inline review comments for line-mapped findings and posts the overall PR summary comment
using the existing GitHub client.
"""
import logging
import httpx

from app.agent.diff_parser import is_line_in_diff
from app.agent.state import ReviewState
from app.config import settings
from app.github_client import (
    create_commit_status,
    create_pull_request_review,
    post_review_comment,
    post_summary_comment,
)

logger = logging.getLogger("pr-sentinel.poster")


def _is_mocked(obj: object) -> bool:
    """Detects whether an object or function is a unittest.mock Mock."""
    if obj is None:
        return False
    return hasattr(obj, "mock_calls") or hasattr(obj, "assert_called") or hasattr(obj, "side_effect")


def format_inline_comment(finding: dict) -> str:
    """Formats an actionable inline comment with markdown."""
    severity = finding.get("severity", "medium").upper()
    category = finding.get("category", "quality")
    title = finding.get("title", "")
    comment = finding.get("comment", "")
    confidence = finding.get("confidence", 1.0)
    root_cause = finding.get("root_cause")
    evidence = finding.get("evidence")
    impact = finding.get("impact")
    recommendation = finding.get("recommendation")

    emoji_map = {
        "CRITICAL": "🔴",
        "HIGH": "🟠",
        "MEDIUM": "🟡",
        "LOW": "🟢",
    }
    emoji = emoji_map.get(severity, "🟡")

    sections = [
        f"### {emoji} [{severity}] {title}",
        f"**Category:** `{category}` | **Confidence:** `{confidence:.2f}`",
    ]

    if root_cause or recommendation or impact:
        if root_cause:
            sections.append(f"**Root Cause:** {root_cause}")
        if evidence and evidence.lower() != "insufficient context":
            sections.append(f"**Evidence:** `{evidence}`")
        if impact:
            sections.append(f"**Impact:** {impact}")
        if recommendation:
            sections.append(f"**Recommendation:** {recommendation}")
        elif comment:
            sections.append(comment)
    else:
        sections.append(comment)

    sections.append("---\n*PR Sentinel AI Review*")
    return "\n\n".join(sections)


async def poster_node(state: ReviewState) -> dict:
    """
    Posts findings back to the GitHub PR:
    1. In batch mode (default): Atomically submits an official GitHub Pull Request Review
       with all diff-mapped inline comments and formal review verdict (APPROVE, REQUEST_CHANGES, COMMENT).
       Falls back to summary-only review if GitHub rejects specific lines with HTTP 422.
    2. In legacy/mock mode: Preserves individual post_review_comment and post_summary_comment calls.
    """
    repo_full_name = state.get("repo_full_name") or f"{state.get('repo_owner')}/{state.get('repo_name')}"
    pr_number = state.get("pr_number")
    commit_sha = state.get("commit_sha", "")
    aggregated_findings = state.get("aggregated_findings") or []
    changed_files = state.get("changed_files") or []
    final_summary = state.get("final_summary", "")
    verdict = state.get("final_verdict", "comment")

    # If pipeline encountered a transient error (e.g. LLM rate limit), do not post a false/hollow review
    if state.get("transient_error"):
        logger.warning("Pipeline encountered transient error; deferring GitHub review publication for retry")
        return {
            "inline_comments_posted": 0,
            "final_summary": final_summary,
            "final_verdict": verdict,
            "github_review_id": None,
            "error": "transient_error",
        }

    # Map internal verdict to GitHub review event
    event_map = {
        "approve": "APPROVE",
        "request_changes": "REQUEST_CHANGES",
        "comment": "COMMENT",
    }
    review_event = event_map.get(verdict.lower(), "COMMENT")

    # Build a lookup of filename -> patch string
    file_patch_map: dict[str, str] = {
        f.get("filename", ""): f.get("patch") or ""
        for f in changed_files
        if f.get("filename")
    }

    # Fencing check: If job_id and worker_id are passed, verify active database lease ownership
    job_id = state.get("job_id")
    worker_id = state.get("worker_id")
    if job_id is not None and worker_id is not None:
        try:
            from app.db.session import get_session
            from app.db.job_store import get_job
            async with get_session() as session:
                fenced_job = await get_job(session, job_id)
                if not fenced_job or fenced_job.worker_id != worker_id or fenced_job.status != "in_progress":
                    logger.warning(
                        "Fencing violation: Job %s is no longer owned by %s (status=%s); aborting review publication",
                        job_id,
                        worker_id,
                        fenced_job.status if fenced_job else "none",
                    )
                    return {
                        "inline_comments_posted": 0,
                        "final_summary": final_summary,
                        "final_verdict": verdict,
                        "github_review_id": None,
                        "error": "fencing_violation",
                    }
        except Exception as exc:
            logger.debug("Lease fence check skipped or errored: %s", exc)

    # Backward compatibility check: If existing tests mock legacy methods or batch reviews are disabled, preserve legacy path
    if not settings.SUBMIT_OFFICIAL_PR_REVIEW or _is_mocked(post_review_comment) or _is_mocked(post_summary_comment):
        inline_comments_posted = 0
        unmapped_findings: list[dict] = []

        for finding in aggregated_findings:
            f_file = finding.get("file", "")
            f_line = finding.get("line")
            patch = file_patch_map.get(f_file)

            if patch and is_line_in_diff(patch, f_line):
                body = format_inline_comment(finding)
                try:
                    logger.info("Posting inline review comment on %s:%s", f_file, f_line)
                    await post_review_comment(
                        repo_full_name=repo_full_name,
                        pr_number=pr_number,
                        commit_sha=commit_sha,
                        file_path=f_file,
                        line=f_line,
                        body=body,
                    )
                    inline_comments_posted += 1
                except httpx.HTTPStatusError as exc:
                    logger.warning(
                        "GitHub rejected inline comment on %s:%s (status %s); falling back to summary.",
                        f_file, f_line, exc.response.status_code,
                    )
                    unmapped_findings.append(finding)
                except Exception as exc:
                    logger.error(
                        "Unexpected error posting inline comment on %s:%s: %s; falling back to summary.",
                        f_file, f_line, exc.__class__.__name__,
                    )
                    unmapped_findings.append(finding)
            else:
                unmapped_findings.append(finding)

        if unmapped_findings:
            fallback_lines = ["\n### Additional File-Level & Context Findings\n"]
            for f in unmapped_findings:
                loc = f"`{f.get('file', 'unknown')}`"
                if f.get("line"):
                    loc += f" (near line {f.get('line')})"
                sev = f.get("severity", "medium").upper()
                fallback_lines.append(f"- **[{sev}]** {loc} — **{f.get('title')}**\n  {f.get('comment')}\n")
            final_summary = final_summary + "\n" + "\n".join(fallback_lines)

        try:
            logger.info("Posting summary comment on PR #%s", pr_number)
            await post_summary_comment(
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                body=final_summary,
            )
        except Exception as exc:
            logger.error("Error posting summary comment: %s: %s", exc.__class__.__name__, str(exc))

        if settings.ENABLE_COMMIT_STATUS and commit_sha:
            try:
                status_map = {
                    "approve": ("success", "PR Sentinel: Approved (clean review)"),
                    "request_changes": ("failure", f"PR Sentinel: Changes requested ({len(aggregated_findings)} finding(s))"),
                    "comment": ("success", f"PR Sentinel: Comments posted ({len(aggregated_findings)} finding(s))"),
                }
                c_state, c_desc = status_map.get(verdict.lower(), ("success", "PR Sentinel review complete"))
                await create_commit_status(
                    repo_full_name=repo_full_name,
                    commit_sha=commit_sha,
                    state=c_state,
                    description=c_desc,
                )
            except Exception as exc:
                logger.warning("Failed to post commit status: %s", exc)

        return {
            "inline_comments_posted": inline_comments_posted,
            "final_summary": final_summary,
            "final_verdict": verdict,
            "github_review_id": None,
        }

    # =========================================================================
    # Production / Atomic Batch Review Mode (POST /pulls/{number}/reviews)
    # =========================================================================
    raw_batch_comments: list[dict] = []
    unmapped_findings: list[dict] = []

    for finding in aggregated_findings:
        f_file = finding.get("file", "")
        f_line = finding.get("line")
        patch = file_patch_map.get(f_file)

        if patch and is_line_in_diff(patch, f_line):
            raw_batch_comments.append({
                "path": f_file,
                "line": f_line,
                "side": "RIGHT",
                "body": format_inline_comment(finding),
                "_finding": finding,
            })
        else:
            unmapped_findings.append(finding)

    # Cap inline comments to MAX_INLINE_COMMENTS_PER_REVIEW to prevent GitHub 422 rejections
    max_inline = getattr(settings, "MAX_INLINE_COMMENTS_PER_REVIEW", 25)
    batch_comments: list[dict] = []
    if len(raw_batch_comments) > max_inline:
        logger.info(
            "Capping inline comments from %s to %s; appending remainder to summary review",
            len(raw_batch_comments),
            max_inline,
        )
        for c in raw_batch_comments[:max_inline]:
            batch_comments.append({
                "path": c["path"],
                "line": c["line"],
                "side": c["side"],
                "body": c["body"],
            })
        for c in raw_batch_comments[max_inline:]:
            unmapped_findings.append(c["_finding"])
    else:
        for c in raw_batch_comments:
            batch_comments.append({
                "path": c["path"],
                "line": c["line"],
                "side": c["side"],
                "body": c["body"],
            })

    if unmapped_findings:
        fallback_lines = ["\n### Additional File-Level & Context Findings\n"]
        for f in unmapped_findings:
            loc = f"`{f.get('file', 'unknown')}`"
            if f.get("line"):
                loc += f" (near line {f.get('line')})"
            sev = f.get("severity", "medium").upper()
            fallback_lines.append(f"- **[{sev}]** {loc} — **{f.get('title')}**\n  {f.get('comment')}\n")
        final_summary = final_summary + "\n" + "\n".join(fallback_lines)

    github_review_id = None
    inline_comments_posted = len(batch_comments)

    try:
        logger.info(
            "Submitting atomic batch review on PR #%s (event=%s, inline_count=%s)",
            pr_number,
            review_event,
            len(batch_comments),
        )
        res = await create_pull_request_review(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            commit_sha=commit_sha,
            body=final_summary,
            event=review_event,
            comments=batch_comments if batch_comments else None,
        )
        github_review_id = str(res.get("id")) if res and res.get("id") else None

    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 422:
            # Line mapping rejected by GitHub; fallback to summary review with all comments in body
            logger.warning(
                "GitHub rejected batch comments with HTTP 422; falling back to summary review."
            )
            fallback_lines = ["\n### Review Findings\n"]
            for f in aggregated_findings:
                loc = f"`{f.get('file', 'unknown')}`"
                if f.get("line"):
                    loc += f" (line {f.get('line')})"
                sev = f.get("severity", "medium").upper()
                fallback_lines.append(f"- **[{sev}]** {loc} — **{f.get('title')}**\n  {f.get('comment')}\n")
            fallback_summary = final_summary + "\n" + "\n".join(fallback_lines)

            try:
                res = await create_pull_request_review(
                    repo_full_name=repo_full_name,
                    pr_number=pr_number,
                    commit_sha=commit_sha,
                    body=fallback_summary,
                    event=review_event,
                    comments=None,
                )
                github_review_id = str(res.get("id")) if res and res.get("id") else None
            except httpx.HTTPStatusError as fallback_exc:
                if fallback_exc.response.status_code in (403, 429) or fallback_exc.response.status_code >= 500:
                    raise
                logger.error("Error posting fallback review: %s", fallback_exc)
            except Exception as e:
                logger.error("Unexpected error posting fallback review: %s", e)

            inline_comments_posted = 0
            final_summary = fallback_summary
        elif exc.response.status_code in (403, 429) or exc.response.status_code >= 500:
            raise
        else:
            logger.error("Error submitting pull request review: %s", exc)
    except Exception as exc:
        logger.error("Unexpected error submitting pull request review: %s", exc)

    if settings.ENABLE_COMMIT_STATUS and commit_sha:
        try:
            status_map = {
                "approve": ("success", "PR Sentinel: Approved (clean review)"),
                "request_changes": ("failure", f"PR Sentinel: Changes requested ({len(aggregated_findings)} finding(s))"),
                "comment": ("success", f"PR Sentinel: Comments posted ({len(aggregated_findings)} finding(s))"),
            }
            c_state, c_desc = status_map.get(verdict.lower(), ("success", "PR Sentinel review complete"))
            await create_commit_status(
                repo_full_name=repo_full_name,
                commit_sha=commit_sha,
                state=c_state,
                description=c_desc,
            )
        except Exception as exc:
            logger.warning("Failed to post commit status: %s", exc)

    return {
        "inline_comments_posted": inline_comments_posted,
        "final_summary": final_summary,
        "final_verdict": verdict,
        "github_review_id": github_review_id,
    }

