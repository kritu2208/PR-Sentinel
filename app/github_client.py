"""
All direct communication with the GitHub API lives here.
Keeping it isolated means when we later swap PAT auth for GitHub App auth,
only this file changes — nothing else needs to know how auth works.
"""
import base64
import logging
import httpx
from app.config import settings

logger = logging.getLogger("pr-sentinel.github")


def _headers() -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if settings.GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {settings.GITHUB_TOKEN}"
    return headers


def _timeout() -> httpx.Timeout:
    """Returns the configured HTTP timeout for GitHub API calls."""
    return httpx.Timeout(settings.GITHUB_REQUEST_TIMEOUT)


async def fetch_pr_files(repo_full_name: str, pr_number: int) -> list[dict]:
    """
    Returns the list of changed files in a PR, each with its patch (diff text).
    Paginates through GitHub API until all changed files across all pages are fetched.

    Example of one item in the response:
    {
        "filename": "app/main.py",
        "status": "modified",
        "additions": 12,
        "deletions": 3,
        "patch": "@@ -10,6 +10,7 @@ ... actual diff text ..."
    }
    """
    url = f"{settings.GITHUB_API_BASE}/repos/{repo_full_name}/pulls/{pr_number}/files"
    all_files: list[dict] = []
    page = 1
    per_page = 100

    async with httpx.AsyncClient(timeout=_timeout()) as client:
        while True:
            params = {"page": page, "per_page": per_page}
            response = await client.get(url, headers=_headers(), params=params)
            response.raise_for_status()
            files_page = response.json()

            if not files_page or not isinstance(files_page, list):
                break

            all_files.extend(files_page)

            # If fewer items than per_page were returned, we have reached the last page
            if len(files_page) < per_page:
                break

            page += 1

    return all_files


async def fetch_file_content(
    repo_full_name: str,
    file_path: str,
    ref: str | None = None,
) -> str | None:
    """
    Fetches the content of a file from a repository at a given ref (commit SHA or branch).
    Uses GitHub Contents API: GET /repos/{owner}/{repo}/contents/{path}?ref={ref}
    Returns the decoded UTF-8 string content, or None if not found/error.
    """
    cleaned_path = file_path.lstrip("/")
    url = f"{settings.GITHUB_API_BASE}/repos/{repo_full_name}/contents/{cleaned_path}"
    params = {}
    if ref:
        params["ref"] = ref

    try:
        async with httpx.AsyncClient(timeout=_timeout()) as client:
            response = await client.get(url, headers=_headers(), params=params)
            if response.status_code == 404:
                logger.debug("File %s not found in %s (ref=%s)", cleaned_path, repo_full_name, ref)
                return None
            if response.status_code != 200:
                logger.warning(
                    "GitHub API contents error for %s in %s: %s %s",
                    cleaned_path,
                    repo_full_name,
                    response.status_code,
                    response.text[:200],
                )
                return None

            data = response.json()
            if isinstance(data, dict) and data.get("encoding") == "base64" and "content" in data:
                raw_b64 = data["content"].replace("\n", "").replace("\r", "")
                content_bytes = base64.b64decode(raw_b64.encode("ascii"))
                return content_bytes.decode("utf-8", errors="replace")
            elif isinstance(data, str):
                return data
            elif isinstance(data, dict) and "content" in data and not data.get("encoding"):
                return str(data["content"])
            return None
    except Exception as exc:
        logger.warning("Exception fetching file content for %s: %s", cleaned_path, exc)
        return None


async def post_review_comment(
    repo_full_name: str,
    pr_number: int,
    commit_sha: str,
    file_path: str,
    line: int,
    body: str,
) -> dict:
    """
    Posts a single inline comment on a specific line of a specific file in the PR.
    This is what makes the review show up exactly like a human reviewer's comment would.
    """
    url = f"{settings.GITHUB_API_BASE}/repos/{repo_full_name}/pulls/{pr_number}/comments"
    payload = {
        "body": body,
        "commit_id": commit_sha,
        "path": file_path,
        "line": line,
        "side": "RIGHT",  # comment on the new version of the code, not the old
    }
    async with httpx.AsyncClient(timeout=_timeout()) as client:
        response = await client.post(url, headers=_headers(), json=payload)
        response.raise_for_status()
        return response.json()


async def post_summary_comment(repo_full_name: str, pr_number: int, body: str) -> dict:
    """
    Posts the overall summary comment at the bottom of the PR
    (not tied to a specific line) — e.g. "Reviewed 5 files, found 3 issues."
    """
    url = f"{settings.GITHUB_API_BASE}/repos/{repo_full_name}/issues/{pr_number}/comments"
    async with httpx.AsyncClient(timeout=_timeout()) as client:
        response = await client.post(url, headers=_headers(), json={"body": body})
        response.raise_for_status()
        return response.json()


async def create_pull_request_review(
    repo_full_name: str,
    pr_number: int,
    commit_sha: str,
    body: str,
    event: str = "COMMENT",
    comments: list[dict] | None = None,
) -> dict:
    """
    Submits a batch review to the GitHub Pull Request Reviews API:
    POST /repos/{owner}/{repo}/pulls/{pr_number}/reviews
    """
    url = f"{settings.GITHUB_API_BASE}/repos/{repo_full_name}/pulls/{pr_number}/reviews"
    payload = {
        "commit_id": commit_sha,
        "body": body,
        "event": event,
    }
    if comments:
        payload["comments"] = comments

    async with httpx.AsyncClient(timeout=_timeout()) as client:
        response = await client.post(url, headers=_headers(), json=payload)
        response.raise_for_status()
        return response.json()


async def create_commit_status(
    repo_full_name: str,
    commit_sha: str,
    state: str,
    description: str,
    context: str = "pr-sentinel/review",
    target_url: str | None = None,
) -> dict:
    """
    Sets a commit status on a specific SHA via GitHub Statuses API:
    POST /repos/{owner}/{repo}/statuses/{sha}
    Used to enforce PR check runs and branch protection rules.
    State must be one of: 'pending', 'success', 'failure', 'error'.
    """
    url = f"{settings.GITHUB_API_BASE}/repos/{repo_full_name}/statuses/{commit_sha}"
    payload: dict = {
        "state": state,
        "description": description[:140],  # GitHub enforces max 140 chars
        "context": context,
    }
    if target_url:
        payload["target_url"] = target_url

    async with httpx.AsyncClient(timeout=_timeout()) as client:
        response = await client.post(url, headers=_headers(), json=payload)
        response.raise_for_status()
        return response.json()


def parse_rate_limit_headers(response: httpx.Response, now_ts: float | None = None) -> float | None:
    """
    Extracts retry delay in seconds if the response indicates rate limiting (403 or 429).
    Checks Retry-After header (seconds or RFC 7231 HTTP-date) and X-RateLimit-Reset timestamp.
    Returns float delay seconds, or None if not rate limited.
    """
    if response.status_code not in (403, 429):
        return None

    import time
    if now_ts is None:
        now_ts = time.time()

    headers = response.headers

    # 1. Retry-After header (delta-seconds or RFC 7231 HTTP-date)
    if "retry-after" in headers:
        val = headers["retry-after"].strip()
        try:
            return max(float(val), 1.0)
        except (ValueError, TypeError):
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(val)
                delay = dt.timestamp() - now_ts
                return max(delay, 1.0)
            except Exception:
                pass

    # 2. X-RateLimit-Remaining == 0 with X-RateLimit-Reset
    remaining = headers.get("x-ratelimit-remaining")
    reset_ts = headers.get("x-ratelimit-reset")
    if remaining == "0" and reset_ts:
        try:
            reset_epoch = float(reset_ts)
            delay = reset_epoch - now_ts
            return max(delay, 1.0)
        except (ValueError, TypeError):
            pass

    # 3. If 429 or secondary 403 without explicit header, provide sensible cooldown
    if response.status_code == 429 or "rate limit" in response.text.lower():
        return 60.0

    return None

