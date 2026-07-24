"""Tests for the unified diff parser and finding validator."""

from __future__ import annotations

from code_review_agent.diff_parser import HunkRange, parse_unified_diff, validate_finding
from code_review_agent.models import Finding

# --- Section 1: parse_unified_diff happy path ----------------------------


def test_single_file_single_hunk() -> None:
    diff = (
        "diff --git a/main.py b/main.py\n"
        "@@ -10,5 +12,7 @@\n context\n-new\n+new line\n+another\n"
    )
    result = parse_unified_diff(diff)
    assert "main.py" in result
    assert len(result["main.py"]) == 1
    assert result["main.py"][0] == HunkRange(start_line=12, line_count=7)


def test_single_file_multiple_hunks() -> None:
    diff = "diff --git a/main.py b/main.py\n@@ -1,3 +1,3 @@\n-a\n+b\n@@ -10,3 +20,5 @@\n+c\n+d\n"
    result = parse_unified_diff(diff)
    assert len(result["main.py"]) == 2
    assert result["main.py"][0] == HunkRange(start_line=1, line_count=3)
    assert result["main.py"][1] == HunkRange(start_line=20, line_count=5)


def test_multiple_files() -> None:
    diff = (
        "diff --git a/foo.py b/foo.py\n@@ -1,1 +1,1 @@\n-a\n+b\n"
        "diff --git a/bar.py b/bar.py\n@@ -5,3 +6,3 @@\n-c\n+d\n"
    )
    result = parse_unified_diff(diff)
    assert set(result.keys()) == {"foo.py", "bar.py"}
    assert result["foo.py"][0] == HunkRange(start_line=1, line_count=1)
    assert result["bar.py"][0] == HunkRange(start_line=6, line_count=3)


# --- Section 2: parse_unified_diff hunk format ---------------------------


def test_short_hunk_format_single_line() -> None:
    # Short form: @@ -a +c @@ — default line_count is 1.
    diff = "diff --git a/file.py b/file.py\n@@ -5 +6 @@\n-a\n+b\n"
    result = parse_unified_diff(diff)
    assert result["file.py"][0] == HunkRange(start_line=6, line_count=1)


def test_long_hunk_format_explicit() -> None:
    diff = "diff --git a/file.py b/file.py\n@@ -10,5 +12,7 @@\n context\n"
    result = parse_unified_diff(diff)
    assert result["file.py"][0] == HunkRange(start_line=12, line_count=7)


def test_mixed_hunk_formats_same_file() -> None:
    diff = "diff --git a/file.py b/file.py\n@@ -1 +2 @@\n@@ -10,4 +20,4 @@\n"
    result = parse_unified_diff(diff)
    assert len(result["file.py"]) == 2
    assert result["file.py"][0] == HunkRange(start_line=2, line_count=1)
    assert result["file.py"][1] == HunkRange(start_line=20, line_count=4)


# --- Section 3: parse_unified_diff edge cases ----------------------------


def test_delete_only_hunk_skipped() -> None:
    # @@ -10,5 +0,0 @@ has start_line=0 → skip (no post-state lines).
    diff = "diff --git a/dead.py b/dead.py\n@@ -10,5 +0,0 @@\n-a\n-b\n-c\n-d\n-e\n"
    result = parse_unified_diff(diff)
    # No hunk_map entry for this file (all deleted, no + post-state).
    assert "dead.py" not in result


def test_empty_input_returns_empty_dict() -> None:
    assert parse_unified_diff("") == {}


def test_non_diff_string_returns_empty_dict() -> None:
    assert parse_unified_diff("hello world\nnot a diff\n") == {}


def test_file_with_no_hunks_not_in_map() -> None:
    # File section with no @@ header lines (e.g., rename with no content changes) → not in map.
    diff = (
        "diff --git a/old.py b/new.py\n"
        "similarity index 100%\nrename from a/old.py\nrename to b/new.py\n"
    )
    result = parse_unified_diff(diff)
    assert "new.py" not in result
    assert result == {}


def test_nested_path_extracted_from_b() -> None:
    # Full nested path stored as key (not basename).
    diff = "diff --git a/src/deep/file.py b/src/deep/file.py\n@@ -1,1 +1,1 @@\n-a\n+b\n"
    result = parse_unified_diff(diff)
    assert "src/deep/file.py" in result


def test_git_quoted_path_unescaped() -> None:
    # Git quotes paths with spaces. Verify _unescape_git_path strips the quotes.
    diff = 'diff --git a/"my file.py" b/"my file.py"\n@@ -1,1 +1,1 @@\n-a\n+b\n'
    result = parse_unified_diff(diff)
    assert "my file.py" in result


# --- Section 4: validate_finding happy path ------------------------------


def test_finding_inside_hunk_returns_true() -> None:
    hunk_map = {"main.py": [HunkRange(start_line=10, line_count=5)]}
    finding = Finding(file="main.py", line=12, severity="warning", message="x")
    assert validate_finding(finding, hunk_map) is True


def test_finding_at_hunk_start_returns_true() -> None:
    hunk_map = {"main.py": [HunkRange(start_line=10, line_count=5)]}
    finding = Finding(file="main.py", line=10, severity="warning", message="x")
    assert validate_finding(finding, hunk_map) is True


def test_finding_at_hunk_end_returns_true() -> None:
    hunk_map = {"main.py": [HunkRange(start_line=10, line_count=5)]}
    # end = 10 + 5 - 1 = 14
    finding = Finding(file="main.py", line=14, severity="warning", message="x")
    assert validate_finding(finding, hunk_map) is True


def test_finding_in_second_hunk_same_file_returns_true() -> None:
    hunk_map = {
        "main.py": [
            HunkRange(start_line=5, line_count=3),
            HunkRange(start_line=20, line_count=4),
        ]
    }
    finding = Finding(file="main.py", line=22, severity="warning", message="x")
    assert validate_finding(finding, hunk_map) is True


# --- Section 5: validate_finding failures --------------------------------


def test_finding_below_hunk_start_returns_false() -> None:
    hunk_map = {"main.py": [HunkRange(start_line=10, line_count=5)]}
    finding = Finding(file="main.py", line=9, severity="warning", message="x")
    assert validate_finding(finding, hunk_map) is False


def test_finding_above_hunk_end_returns_false() -> None:
    hunk_map = {"main.py": [HunkRange(start_line=10, line_count=5)]}
    # end = 14; line 15 is above the hunk.
    finding = Finding(file="main.py", line=15, severity="warning", message="x")
    assert validate_finding(finding, hunk_map) is False


def test_finding_file_not_in_map_returns_false() -> None:
    hunk_map = {"other.py": [HunkRange(start_line=1, line_count=1)]}
    finding = Finding(file="main.py", line=1, severity="warning", message="x")
    assert validate_finding(finding, hunk_map) is False


def test_finding_line_zero_returns_false() -> None:
    hunk_map = {"main.py": [HunkRange(start_line=10, line_count=5)]}
    finding = Finding(file="main.py", line=0, severity="warning", message="x")
    assert validate_finding(finding, hunk_map) is False


def test_finding_line_negative_returns_false() -> None:
    hunk_map = {"main.py": [HunkRange(start_line=10, line_count=5)]}
    finding = Finding(file="main.py", line=-5, severity="warning", message="x")
    assert validate_finding(finding, hunk_map) is False


def test_finding_file_has_empty_hunk_list_returns_false() -> None:
    # Defensive: file present in map but with an empty hunk list.
    hunk_map: dict[str, list[HunkRange]] = {"main.py": []}
    finding = Finding(file="main.py", line=10, severity="warning", message="x")
    assert validate_finding(finding, hunk_map) is False
