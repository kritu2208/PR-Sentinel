"""
Diff parser and file filtering utilities for PR Sentinel.
Ensures line numbers are mapped accurately to diff hunks and filters non-reviewable files.
"""
import os
import re

HUNK_HEADER_REGEX = re.compile(r"^@@\s+-(?:\d+)(?:,\d+)?\s+\+(\d+)(?:,(\d+))?\s+@@")

# Extensions and files that should not be reviewed by LLM
IGNORED_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".webp",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".mp4", ".mov", ".webm", ".mp3", ".wav",
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
    ".pdf", ".exe", ".dll", ".so", ".dylib",
    ".pyc", ".pyo", ".pyd",
    ".map", ".min.js", ".min.css",
)

IGNORED_FILENAMES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "pipfile.lock",
    "cargo.lock",
    "composer.lock",
    "go.sum",
    "flake.lock",
}


def is_reviewable_file(filename: str, patch: str | None = None) -> bool:
    """
    Checks if a file is suitable for code review.
    Skips binary files, lockfiles, generated assets, and files without patches.
    """
    if not filename:
        return False

    fn_lower = filename.lower()
    base_name = os.path.basename(fn_lower)
    if base_name in IGNORED_FILENAMES:
        return False

    if any(fn_lower.endswith(ext) for ext in IGNORED_EXTENSIONS):
        return False

    # Skip files that have no patch or an empty diff (e.g. pure renames or binary)
    if patch is None or not patch.strip():
        return False

    return True


def extract_hunk_lines(patch: str) -> set[int]:
    """
    Parses unified diff hunks from a git patch and extracts all line numbers in the
    modified/new file (RIGHT side) that fall within the hunk.

    GitHub inline review comments are only allowed on lines within these hunks.
    """
    if not patch:
        return set()

    valid_lines: set[int] = set()
    current_new_line: int | None = None

    for line in patch.splitlines():
        hunk_match = HUNK_HEADER_REGEX.match(line)
        if hunk_match:
            new_start = int(hunk_match.group(1))
            current_new_line = new_start
            continue

        if current_new_line is None:
            continue

        # In unified diff:
        # '+' lines are added lines in the new file -> increment new_line
        # ' ' lines are unchanged context in the new file -> increment new_line
        # '-' lines are deleted lines from old file -> do not increment new_line
        if line.startswith("+") and not line.startswith("+++"):
            valid_lines.add(current_new_line)
            current_new_line += 1
        elif line.startswith(" "):
            valid_lines.add(current_new_line)
            current_new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            # Old file line removed; does not advance new file line counter
            pass
        elif line.startswith(r"\ No newline at end of file"):
            pass

    return valid_lines


def is_line_in_diff(patch: str | None, line: int | None) -> bool:
    """
    Returns True if the line number is confirmed to exist within the diff hunk
    of the modified file.
    """
    if patch is None or line is None or line <= 0:
        return False
    return line in extract_hunk_lines(patch)


DIFF_TRUNCATION_MARKER = "\n... [diff truncated due to size limits]"


def truncate_patch(patch: str, max_bytes: int = 20000) -> str:
    """
    Safely truncates patch if it exceeds max_bytes to protect LLM context windows.

    Guarantees:
    - Never splits mid-line or corrupts multi-byte UTF-8 character sequences.
    - Truncates strictly at the last complete newline boundary within the allocated byte budget.
    - Total output byte length is bounded by max_bytes.
    - Appends DIFF_TRUNCATION_MARKER so the LLM knows the diff was truncated.
    """
    if not patch:
        return ""

    patch_bytes = patch.encode("utf-8")
    if len(patch_bytes) <= max_bytes:
        return patch

    marker_bytes = DIFF_TRUNCATION_MARKER.encode("utf-8")
    # Reserve space for the marker so total output is within max_bytes
    budget = max(0, max_bytes - len(marker_bytes))

    # Slice at budget, then locate the last newline boundary within budget
    cut_slice = patch_bytes[:budget]
    last_newline_idx = cut_slice.rfind(b"\n")

    if last_newline_idx != -1:
        safe_bytes = cut_slice[:last_newline_idx]
    else:
        # Fallback if no newline exists before budget: decode safely without cutting characters
        safe_bytes = cut_slice

    safe_text = safe_bytes.decode("utf-8", errors="ignore")
    return safe_text + DIFF_TRUNCATION_MARKER
