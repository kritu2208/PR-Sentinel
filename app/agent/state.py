"""
Typed LangGraph state definition for the PR Review pipeline.
"""
from typing import Literal, TypedDict


class ReviewState(TypedDict, total=False):
    repo_owner: str
    repo_name: str
    repo_full_name: str
    pr_number: int
    pr_title: str
    pr_url: str
    commit_sha: str
    changed_files: list[dict]
    reviewed_files: list[str]
    retrieved_context: list[dict]
    raw_findings: list[dict]
    validated_findings: list[dict]
    investigated_findings: list[dict]
    aggregated_findings: list[dict]
    final_summary: str
    final_verdict: Literal["approve", "comment", "request_changes"]
    inline_comments_posted: int
    github_review_id: str | None
    job_id: int | None
    worker_id: str | None
    error: str | None
    transient_error: bool | None

