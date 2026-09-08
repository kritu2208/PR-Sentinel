"""
GitHub sends a huge JSON payload on every webhook event.
We only care about a handful of fields, so we define a slim model
instead of parsing the entire thing.
"""
from pydantic import BaseModel


class Repository(BaseModel):
    full_name: str  # e.g. "octocat/Hello-World"


class CommitRef(BaseModel):
    sha: str


class PullRequest(BaseModel):
    number: int
    title: str
    diff_url: str
    html_url: str | None = None
    head: CommitRef


class PullRequestWebhookPayload(BaseModel):
    action: str  # "opened", "synchronize", "closed", etc.
    number: int
    pull_request: PullRequest
    repository: Repository
