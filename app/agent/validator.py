"""
Validator Node for PR Sentinel.
Validates raw findings from the Analyzer against PR changed files and diff hunks
to prevent hallucinated files or line numbers from reaching the Aggregator.
"""
import logging
from pydantic import ValidationError

from app.agent.diff_parser import is_line_in_diff
from app.agent.schemas import Finding
from app.agent.state import ReviewState

logger = logging.getLogger("pr-sentinel.validator")


async def validator_node(state: ReviewState) -> dict:
    """
    Validates every item in state["raw_findings"].

    A. Validate that 'file' exactly matches a filename in state['changed_files'].
    B. If finding['line'] is not None:
       - Find the corresponding patch for that exact file.
       - Validate the line using is_line_in_diff(patch, line).
       - Reject if line is invalid or outside the diff hunk.
    C. If line is None:
       - Allow it as a file-level/context finding, provided the file itself exists in changed_files.
    D. Validate the finding against the existing Pydantic Finding schema where appropriate.
    E. Reject malformed findings safely without crashing the pipeline.
    F. Preserve valid findings unchanged.
    G. Log how many findings were accepted and rejected (no secrets).

    Returns:
    {
        "validated_findings": [...]
    }
    """
    raw_findings = state.get("raw_findings") or []
    changed_files = state.get("changed_files") or []

    # Map filename -> original patch from changed_files
    file_patch_map: dict[str, str | None] = {}
    for f in changed_files:
        if isinstance(f, dict) and f.get("filename"):
            file_patch_map[f["filename"]] = f.get("patch")

    # Only accept findings for files actually reviewed by the Analyzer
    raw_reviewed = state.get("reviewed_files")
    if raw_reviewed is not None:
        reviewed_file_set = set(raw_reviewed)
    else:
        # Fallback for tests/states where reviewed_files is not explicitly specified
        reviewed_file_set = set(file_patch_map.keys())

    validated_findings: list[dict] = []
    rejected_count = 0

    for item in raw_findings:
        # D & E: Validate schema safely
        if not isinstance(item, (dict, Finding)):
            rejected_count += 1
            continue

        try:
            if isinstance(item, Finding):
                finding_obj = item
                finding_dict = item.model_dump()
            else:
                finding_obj = Finding.model_validate(item)
                finding_dict = dict(item)
        except (ValidationError, Exception) as exc:
            logger.warning(f"Rejecting malformed finding schema: {exc.__class__.__name__}")
            rejected_count += 1
            continue

        # A: Validate that 'file' was actually sent to Analyzer and exists in changed_files
        target_file = finding_obj.file
        if target_file not in reviewed_file_set or target_file not in file_patch_map:
            logger.warning(
                f"Rejecting finding on unreviewed or unknown file: {target_file}"
            )
            rejected_count += 1
            continue

        target_line = finding_obj.line
        # Original patch from changed_files is used for line validation
        patch = file_patch_map.get(target_file)

        # B & C: Line number validation
        if target_line is not None:
            if not is_line_in_diff(patch, target_line):
                logger.warning(
                    f"Rejecting finding on '{target_file}': line {target_line} is outside diff hunk or invalid."
                )
                rejected_count += 1
                continue
        else:
            # Line is None: allowed as file-level finding since file exists in changed_files
            pass

        # F: Preserve valid finding unchanged
        validated_findings.append(finding_dict)

    # G: Log accepted and rejected counts without leaking secrets
    logger.info(
        f"Validator processed {len(raw_findings)} raw finding(s): "
        f"{len(validated_findings)} accepted, {rejected_count} rejected."
    )

    return {"validated_findings": validated_findings}
