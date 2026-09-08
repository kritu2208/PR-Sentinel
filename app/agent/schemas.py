"""
Pydantic schemas for structured inputs and outputs in the review pipeline.
Hardened with field-level constraints to prevent oversized or malformed LLM outputs.
"""
from typing import Literal
from pydantic import BaseModel, Field

SeverityLevel = Literal["critical", "high", "medium", "low"]
CategoryType = Literal["bug", "security", "performance", "testing", "quality"]
VerdictType = Literal["approve", "comment", "request_changes"]


class Finding(BaseModel):
    file: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Exact path of the changed file relative to repository root (e.g. 'app/main.py').",
    )
    line: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Exact line number in the modified/new file (RIGHT side) within the diff hunk. "
            "Set to null if the issue spans the whole file or cannot be tied to a specific patch line."
        ),
    )
    severity: SeverityLevel = Field(
        ...,
        description="Severity level of the issue: 'critical', 'high', 'medium', or 'low'.",
    )
    category: CategoryType = Field(
        ...,
        description="Category of the finding: 'bug', 'security', 'performance', 'testing', or 'quality'.",
    )
    title: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Short, descriptive title summarizing the issue (under 200 characters).",
    )
    comment: str = Field(
        ...,
        min_length=1,
        max_length=5000,
        description=(
            "Clear explanation of why this is a problem and an actionable suggestion or fix. "
            "Never quote or reproduce raw secrets, passwords, or credentials."
        ),
    )
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Confidence score in the finding between 0.0 and 1.0.",
    )
    root_cause: str | None = Field(
        default=None,
        description="Technical root cause of the issue verified against diff and retrieved context.",
    )
    evidence: str | None = Field(
        default=None,
        description="Concrete supporting evidence from diff or repository context.",
    )
    impact: str | None = Field(
        default=None,
        description="Potential functional, operational, or security impact.",
    )
    recommendation: str | None = Field(
        default=None,
        description="Actionable recommended fix or remediation.",
    )
    investigation_status: Literal["confirmed", "uncertain", "skipped", "failed"] | None = Field(
        default=None,
        description="Investigation verification status.",
    )


class AnalyzerOutput(BaseModel):
    findings: list[Finding] = Field(
        default_factory=list,
        description="List of actionable code review findings. Empty if no meaningful issues are found.",
    )


class InvestigationItem(BaseModel):
    file: str = Field(..., description="Exact file path of the finding being investigated.")
    line: int | None = Field(default=None, description="Line number matching the finding, or null.")
    title: str = Field(..., description="Title matching the finding.")
    root_cause: str = Field(
        ...,
        description="Concise, precise technical root cause verified against diffs and retrieved context.",
    )
    evidence: str = Field(
        ...,
        description="Concrete code evidence supporting this conclusion. If evidence is lacking, state 'Insufficient context'.",
    )
    impact: str = Field(
        ...,
        description="Realistic technical, security, or business impact.",
    )
    recommendation: str = Field(
        ...,
        description="Specific, actionable code fix or remediation. Never leak secrets.",
    )
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Confidence in the investigation conclusion between 0.0 and 1.0.",
    )
    status: Literal["confirmed", "uncertain"] = Field(
        default="confirmed",
        description="'confirmed' if evidence conclusively proves root cause, 'uncertain' if evidence is incomplete.",
    )


class InvestigatorOutput(BaseModel):
    investigations: list[InvestigationItem] = Field(
        default_factory=list,
        description="Root cause investigation results for the validated findings.",
    )


class AggregatorOutput(BaseModel):
    verdict: VerdictType = Field(
        ...,
        description="Final review verdict: 'approve', 'comment', or 'request_changes'.",
    )
    summary: str = Field(
        ...,
        description="Executive summary of the review findings and PR health.",
    )
    findings: list[Finding] = Field(
        default_factory=list,
        description="Deduplicated and prioritized list of findings.",
    )
