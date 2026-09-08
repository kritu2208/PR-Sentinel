"""
Tests for diff parser and file filter utilities.
"""
from app.agent.diff_parser import (
    extract_hunk_lines,
    is_line_in_diff,
    is_reviewable_file,
    truncate_patch,
)

SAMPLE_PATCH = """@@ -10,4 +10,6 @@ def example():
     x = 1
-    y = 2
+    y = 20
+    z = 30
     return x + y
"""


def test_extract_hunk_lines():
    """Verifies that line numbers in the modified file hunk are extracted accurately."""
    lines = extract_hunk_lines(SAMPLE_PATCH)
    # Hunk starts at new line 10.
    # Line 10: "    x = 1" (context -> 10)
    # Line 11: "+    y = 20" (added -> 11)
    # Line 12: "+    z = 30" (added -> 12)
    # Line 13: "    return x + y" (context -> 13)
    assert 10 in lines
    assert 11 in lines
    assert 12 in lines
    assert 13 in lines
    assert 9 not in lines
    assert 14 not in lines


def test_is_line_in_diff():
    """Verifies line membership in diff hunks."""
    assert is_line_in_diff(SAMPLE_PATCH, 11) is True
    assert is_line_in_diff(SAMPLE_PATCH, 999) is False
    assert is_line_in_diff(SAMPLE_PATCH, None) is False
    assert is_line_in_diff("", 10) is False


def test_is_reviewable_file():
    """Verifies non-code and binary files are filtered out."""
    assert is_reviewable_file("app/main.py", "@@ -1 +1 @@") is True
    assert is_reviewable_file("package-lock.json", "@@ -1 +1 @@") is False
    assert is_reviewable_file("yarn.lock", "@@ -1 +1 @@") is False
    assert is_reviewable_file("assets/logo.png", "@@ -1 +1 @@") is False
    assert is_reviewable_file("bundle.min.js", "@@ -1 +1 @@") is False
    assert is_reviewable_file("app/main.py", "") is False
    assert is_reviewable_file("app/main.py", None) is False


def test_truncate_patch():
    """Verifies large diffs are truncated gracefully."""
    small_patch = "@@ -1,2 +1,2 @@\n+test\n"
    assert truncate_patch(small_patch, 100) == small_patch

    huge_patch = "x" * 500
    truncated = truncate_patch(huge_patch, 100)
    assert len(truncated.encode("utf-8")) <= 100
    assert "... [diff truncated due to size limits]" in truncated


def test_truncate_patch_preserves_complete_lines():
    """Truncation preserves complete lines and cuts at newline boundary."""
    multi_line_patch = (
        "@@ -1,10 +1,10 @@\n"
        "+line1: first added line\n"
        "+line2: second added line\n"
        "+line3: third added line\n"
        "+line4: fourth added line\n"
    )
    # Truncate with budget that fits only the first 2 lines plus marker
    truncated = truncate_patch(multi_line_patch, max_bytes=100)
    assert len(truncated.encode("utf-8")) <= 100
    assert "... [diff truncated due to size limits]" in truncated
    # Check that lines before the marker end with a complete newline
    content_part = truncated.split("\n... [diff truncated")[0]
    assert content_part.endswith("+line2: second added line") or content_part.endswith("+line1: first added line")
    # Verify no partial line like "+line3: thi" exists
    assert "+line3: thi" not in truncated


def test_truncate_patch_preserves_multibyte_utf8():
    """Truncation does not corrupt multi-byte UTF-8 characters."""
    # 3-byte and 4-byte characters
    unicode_patch = (
        "@@ -1,5 +1,5 @@\n"
        "+comment = 'Unicode: 🚀 rocket and € euro'\n"
        "+more = '✨ sparkles and 日本語'\n"
        "+final = 'end of diff'\n"
    )
    truncated = truncate_patch(unicode_patch, max_bytes=110)
    assert len(truncated.encode("utf-8")) <= 110
    # Must be valid UTF-8 without raising UnicodeDecodeError
    decoded = truncated.encode("utf-8").decode("utf-8")
    assert "..." in decoded


def test_line_parsing_on_original_untruncated_patch():
    """Confirms diff line parsing continues working accurately on original patches."""
    patch = "@@ -1,3 +1,4 @@\n line1\n+line2\n line3\n"
    lines = extract_hunk_lines(patch)
    assert lines == {1, 2, 3}
    assert is_line_in_diff(patch, 2) is True
    assert is_line_in_diff(patch, 4) is False
