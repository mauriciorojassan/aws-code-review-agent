"""Tests for the diff eligibility filter."""

from __future__ import annotations

from code_review_agent.diff_filter import EligibleDiff, filter_diff


def _section(path: str, content: str | None = None) -> str:
    """Build a minimal ``diff --git`` section for ``path`` with optional body."""
    header = f"diff --git a/{path} b/{path}\n"
    if content is None:
        body = f"--- a/{path}\n+++ b/{path}\n@@ -1,1 +1,1 @@\n-old line\n+new line\n"
    else:
        body = content if content.endswith("\n") else content + "\n"
    return header + body


def _multi(*sections: str) -> str:
    return "".join(sections)


# --- Happy path -----------------------------------------------------------


def test_single_eligible_file_kept() -> None:
    diff = _section("src/main.py")
    result = filter_diff(diff)
    assert result.total_files == 1
    assert result.excluded_count == 0
    assert result.is_empty is False
    assert result.too_large is False
    assert result.content == diff


def test_five_mixed_extensions_all_kept() -> None:
    diff = _multi(
        _section("a.py"),
        _section("b.js"),
        _section("c.ts"),
        _section("d.go"),
        _section("e.java"),
    )
    result = filter_diff(diff)
    assert result.total_files == 5
    assert result.excluded_count == 0
    assert result.is_empty is False
    assert result.too_large is False


# --- Denylist coverage ----------------------------------------------------


def test_package_lock_json_excluded() -> None:
    result = filter_diff(_section("package-lock.json"))
    assert result.excluded_files == ("package-lock.json",)
    assert result.is_empty is True


def test_yarn_lock_excluded() -> None:
    result = filter_diff(_section("yarn.lock"))
    assert result.excluded_files == ("yarn.lock",)


def test_poetry_lock_excluded() -> None:
    result = filter_diff(_section("poetry.lock"))
    assert result.excluded_files == ("poetry.lock",)


def test_cargo_lock_excluded() -> None:
    result = filter_diff(_section("Cargo.lock"))
    assert result.excluded_files == ("Cargo.lock",)


def test_composer_lock_excluded_by_glob() -> None:
    result = filter_diff(_section("composer.lock"))
    assert result.excluded_files == ("composer.lock",)


def test_vendor_min_js_excluded_by_glob() -> None:
    result = filter_diff(_section("vendor.min.js"))
    assert result.excluded_files == ("vendor.min.js",)


def test_styles_min_css_excluded_by_glob() -> None:
    result = filter_diff(_section("styles.min.css"))
    assert result.excluded_files == ("styles.min.css",)


def test_lock_infix_but_not_extension_kept() -> None:
    diff = _section("my-document.lock.md")
    result = filter_diff(diff)
    assert result.excluded_count == 0
    assert result.total_files == 1
    assert "my-document.lock.md" in result.content


def test_dotfile_minified_excluded() -> None:
    result = filter_diff(_section(".min.js"))
    assert result.excluded_files == (".min.js",)
    assert result.is_empty is True


def test_middle_min_excluded() -> None:
    result = filter_diff(_section("my.file.min.txt"))
    assert result.excluded_files == ("my.file.min.txt",)


def test_styles_min_css_regression() -> None:
    # Regression: existing behavior after broadening .*\.min\..* still holds.
    result = filter_diff(_section("styles.min.css"))
    assert result.excluded_files == ("styles.min.css",)


# --- Binary detection -----------------------------------------------------


def test_nul_byte_content_excluded_as_binary() -> None:
    diff = _section("blob.bin", content="binary\x00data")
    result = filter_diff(diff)
    assert result.excluded_files == ("blob.bin",)
    assert result.is_empty is True


def test_invalid_utf8_content_excluded_as_binary() -> None:
    # A lone surrogate cannot be encoded as UTF-8 and raises UnicodeEncodeError.
    diff = _section("weird.bin", content="pre\ud800post")
    result = filter_diff(diff)
    assert result.excluded_files == ("weird.bin",)
    assert result.is_empty is True


# --- Mixed diffs ----------------------------------------------------------


def test_mixed_five_eligible_three_denylisted() -> None:
    diff = _multi(
        _section("src/a.py"),
        _section("package-lock.json"),
        _section("src/b.py"),
        _section("Cargo.lock"),
        _section("src/c.py"),
        _section("vendor.min.js"),
        _section("src/d.py"),
        _section("src/e.py"),
    )
    result = filter_diff(diff)
    assert result.total_files == 8
    assert result.excluded_count == 3
    assert result.excluded_files == (
        "Cargo.lock",
        "package-lock.json",
        "vendor.min.js",
    )
    for keeper in ("src/a.py", "src/b.py", "src/c.py", "src/d.py", "src/e.py"):
        assert keeper in result.content
    for denied in ("package-lock.json", "Cargo.lock", "vendor.min.js"):
        assert denied not in result.content


def test_all_denied_returns_empty_content() -> None:
    diff = _multi(_section("yarn.lock"), _section("Cargo.lock"))
    result = filter_diff(diff)
    assert result.is_empty is True
    assert result.total_files == 2
    assert result.excluded_count == 2
    assert result.excluded_files == ("Cargo.lock", "yarn.lock")
    assert result.content == ""


# --- Gate boundary --------------------------------------------------------


def test_exactly_50_eligible_not_too_large() -> None:
    diff = _multi(*(_section(f"src/f{i}.py") for i in range(50)))
    result = filter_diff(diff)
    assert result.total_files == 50
    assert result.too_large is False
    assert result.is_empty is False


def test_51_eligible_too_large_but_content_present() -> None:
    diff = _multi(*(_section(f"src/f{i}.py") for i in range(51)))
    result = filter_diff(diff)
    assert result.total_files == 51
    assert result.too_large is True
    for i in range(51):
        assert f"src/f{i}.py" in result.content


def test_50_eligible_plus_10_denylisted_not_too_large() -> None:
    eligibles = [_section(f"src/f{i}.py") for i in range(50)]
    denies = [_section(f"lib{i}.lock") for i in range(10)]
    result = filter_diff(_multi(*eligibles, *denies))
    assert result.total_files == 60
    assert result.excluded_count == 10
    assert result.too_large is False


def test_51_eligible_plus_10_denylisted_too_large() -> None:
    eligibles = [_section(f"src/f{i}.py") for i in range(51)]
    denies = [_section(f"lib{i}.lock") for i in range(10)]
    result = filter_diff(_multi(*eligibles, *denies))
    assert result.total_files == 61
    assert result.excluded_count == 10
    assert result.too_large is True


# --- Edge cases -----------------------------------------------------------


def test_empty_input_returns_empty() -> None:
    result = filter_diff("")
    assert result == EligibleDiff(
        content="",
        excluded_files=(),
        excluded_count=0,
        total_files=0,
        is_empty=True,
        too_large=False,
    )


def test_non_diff_string_returns_empty() -> None:
    result = filter_diff("hello world\nthis is not a diff\n")
    assert result == EligibleDiff(
        content="",
        excluded_files=(),
        excluded_count=0,
        total_files=0,
        is_empty=True,
        too_large=False,
    )


def test_deletion_only_diff_is_kept() -> None:
    diff = (
        "diff --git a/deleted.txt b/deleted.txt\n"
        "deleted file mode 100644\n"
        "index abc1234..0000000\n"
        "--- a/deleted.txt\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-line one\n"
        "-line two\n"
    )
    result = filter_diff(diff)
    assert result.total_files == 1
    assert result.excluded_count == 0
    assert "deleted.txt" in result.content


def test_nested_path_basename_extraction() -> None:
    diff = _section("src/deep/nested/file.py")
    result = filter_diff(diff)
    assert result.total_files == 1
    assert result.excluded_count == 0
    assert "src/deep/nested/file.py" in result.content


def test_excluded_files_are_sorted_lexically() -> None:
    diff = _multi(
        _section("zeta.lock"),
        _section("alpha.min.js"),
        _section("keep.py"),
        _section("Cargo.lock"),
        _section("beta.lock"),
    )
    result = filter_diff(diff)
    assert result.excluded_files == (
        "Cargo.lock",
        "alpha.min.js",
        "beta.lock",
        "zeta.lock",
    )
    assert result.total_files == 5
    assert result.excluded_count == 4
    assert "keep.py" in result.content


# --- Git-quoted paths -----------------------------------------------------


def test_path_with_spaces_kept() -> None:
    diff = (
        'diff --git a/"name with spaces.py" b/"name with spaces.py"\n'
        "--- a/x\n+++ b/x\n@@ -1,1 +1,1 @@\n-a\n+b\n"
    )
    result = filter_diff(diff)
    assert result.total_files == 1
    assert result.excluded_count == 0
    assert result.is_empty is False


def test_path_with_embedded_quotes_kept() -> None:
    # Git escapes a literal " inside a quoted path as "".
    diff = (
        'diff --git a/"name""with""quotes.py" b/"name""with""quotes.py"\n'
        "--- a/x\n+++ b/x\n@@ -1,1 +1,1 @@\n-a\n+b\n"
    )
    result = filter_diff(diff)
    assert result.total_files == 1
    assert result.excluded_count == 0


def test_unquoted_path_regression() -> None:
    # Regression: paths without spaces or quotes still parse correctly.
    diff = _section("plain.py")
    result = filter_diff(diff)
    assert result.total_files == 1
    assert result.excluded_count == 0
