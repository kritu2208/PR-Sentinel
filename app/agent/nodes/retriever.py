"""
Retriever Node for PR Sentinel.
Extracts symbols, imports, and call-sites from PR diffs and retrieves targeted
cross-file context from the repository without an external vector database.
"""
import logging
from app.agent.retrieval import RetrievalEngine
from app.agent.state import ReviewState
from app.config import settings
from app.github_client import fetch_file_content

logger = logging.getLogger("pr-sentinel.retriever")


async def retriever_node(state: ReviewState) -> dict:
    """
    Retrieves cross-file context for changed files in the PR:
    1. Extracts newly added or modified imports, definitions, and symbol calls.
    2. Resolves candidate repository paths.
    3. Fetches relevant files from GitHub API at commit_sha.
    4. Slices targeted symbol definitions and bounds byte consumption.
    5. Returns {"retrieved_context": [...]}.

    Fail-safe: Any network, parsing, or GitHub error degrades safely to
    empty context without interrupting the PR review pipeline.
    """
    if not getattr(settings, "ENABLE_CODEBASE_RETRIEVAL", True):
        logger.info("Retriever node: Codebase retrieval disabled via ENABLE_CODEBASE_RETRIEVAL")
        return {"retrieved_context": []}

    repo_full_name = state.get("repo_full_name") or ""
    commit_sha = state.get("commit_sha")
    changed_files = state.get("changed_files") or []

    if not repo_full_name or not changed_files:
        logger.debug("Retriever node: Missing repo_full_name or changed_files; returning empty context")
        return {"retrieved_context": []}

    try:
        engine = RetrievalEngine(fetch_content_fn=fetch_file_content)
        context = await engine.retrieve_context_for_pr(
            repo_full_name=repo_full_name,
            commit_sha=commit_sha,
            changed_files=changed_files,
        )
        return {"retrieved_context": context}
    except Exception as exc:
        logger.warning("Retriever node encountered error; degrading gracefully: %s", exc)
        return {"retrieved_context": []}
