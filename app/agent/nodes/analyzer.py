"""
Analyzer Node for PR Sentinel.
Analyzes PR diffs using a Groq-hosted LLM and returns structured findings.
"""
import logging
import httpx
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_groq import ChatGroq

from app.agent.diff_parser import is_reviewable_file, truncate_patch
from app.agent.prompts import ANALYZER_SYSTEM_PROMPT, build_analyzer_user_prompt
from app.agent.schemas import AnalyzerOutput
from app.agent.state import ReviewState
from app.config import settings

logger = logging.getLogger("pr-sentinel.analyzer")


async def analyzer_node(state: ReviewState) -> dict:
    """
    Analyzes the changed files and diffs in the PR using Groq LLM.
    Returns structured findings categorized by bug, security, performance, etc.
    """
    changed_files = state.get("changed_files") or []

    # Filter reviewable files (skip binary, lockfiles, etc.) and truncate large patches
    reviewable_files = []
    for f in changed_files:
        filename = f.get("filename", "")
        patch = f.get("patch")
        if is_reviewable_file(filename, patch):
            reviewable_files.append({
                **f,
                "patch": truncate_patch(patch or "", settings.MAX_PATCH_BYTES),
            })

    if not reviewable_files:
        logger.info("No reviewable files or diffs found in PR; returning empty findings.")
        return {"raw_findings": [], "reviewed_files": []}

    # Respect maximum file review limit
    if len(reviewable_files) > settings.MAX_FILES_TO_REVIEW:
        logger.warning(
            f"PR contains {len(reviewable_files)} reviewable files; limiting review to first {settings.MAX_FILES_TO_REVIEW}."
        )
        reviewable_files = reviewable_files[:settings.MAX_FILES_TO_REVIEW]

    # Exactly record the files whose diffs are sent to the LLM
    reviewed_file_names = [f["filename"] for f in reviewable_files if f.get("filename")]

    # Validate API key presence
    if not settings.GROQ_API_KEY:
        logger.error("GROQ_API_KEY is not configured. Skipping LLM analysis.")
        return {
            "raw_findings": [],
            "reviewed_files": reviewed_file_names,
            "error": "GROQ_API_KEY is not configured.",
        }

    user_prompt = build_analyzer_user_prompt(
        pr_title=state.get("pr_title", "Pull Request"),
        changed_files=reviewable_files,
        retrieved_context=state.get("retrieved_context") or [],
    )

    messages = [
        SystemMessage(content=ANALYZER_SYSTEM_PROMPT),
        HumanMessage(content=user_prompt),
    ]

    try:
        llm = ChatGroq(
            groq_api_key=settings.GROQ_API_KEY,
            model_name=settings.GROQ_MODEL,
            temperature=0.1,
        )
        structured_llm = llm.with_structured_output(AnalyzerOutput)
        response = await structured_llm.ainvoke(messages)

        if isinstance(response, AnalyzerOutput):
            findings_data = [f.model_dump() for f in response.findings]
        elif isinstance(response, dict):
            parsed = AnalyzerOutput.model_validate(response)
            findings_data = [f.model_dump() for f in parsed.findings]
        else:
            findings_data = []

        logger.info(f"Analyzer identified {len(findings_data)} raw finding(s) across {len(reviewed_file_names)} file(s)")
        return {
            "raw_findings": findings_data,
            "reviewed_files": reviewed_file_names,
        }

    except Exception as exc:
        # Never log API keys or secrets
        logger.error(f"Error during Analyzer LLM invocation: {exc.__class__.__name__}: {str(exc)}")
        err_str = str(exc).lower()
        is_transient = any(
            token in err_str
            for token in (
                "rate limit",
                "429",
                "503",
                "502",
                "504",
                "timeout",
                "connection error",
                "connection reset",
                "temporarily unavailable",
                "overloaded",
            )
        ) or isinstance(exc, (TimeoutError, httpx.TimeoutException, httpx.ConnectError))
        return {
            "raw_findings": [],
            "reviewed_files": reviewed_file_names,
            "error": f"Analyzer LLM invocation failed: {exc.__class__.__name__}",
            "transient_error": is_transient,
        }

