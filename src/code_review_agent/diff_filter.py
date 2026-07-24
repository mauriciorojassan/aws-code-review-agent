"""Filter a unified diff to exclude denylisted and binary files before analysis."""

from __future__ import annotations

import re
from dataclasses import dataclass

_PATH = r'(?:"(?:""|[^"])*"|\S+)'
_HEADER_RE = re.compile(rf"^diff --git a/({_PATH}) b/({_PATH})", re.MULTILINE)

# Denylist matches basenames only.
#   - package-lock.json (exact match)
#   - anything ending in .lock (yarn.lock, poetry.lock, Cargo.lock, composer.lock, .lock, ...)
#   - anything containing .min. as an extension (vendor.min.js, styles.min.css, .min.js, ...)
_DENYLIST_RE = re.compile(r"^(package-lock\.json|.*\.lock|.*\.min\..*)$")


@dataclass(frozen=True, slots=True)
class EligibleDiff:
    """Filtered unified diff plus metadata about exclusions and gating."""

    content: str
    excluded_files: tuple[str, ...]
    excluded_count: int
    total_files: int
    is_empty: bool
    too_large: bool


_EMPTY = EligibleDiff(
    content="",
    excluded_files=(),
    excluded_count=0,
    total_files=0,
    is_empty=True,
    too_large=False,
)


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _unescape_git_path(path: str) -> str:
    """Unquote a Git-quoted path. ``"foo""bar"`` → ``foo"bar``."""
    if len(path) >= 2 and path.startswith('"') and path.endswith('"'):
        return path[1:-1].replace('""', '"')
    return path


def _is_denylisted(basename: str) -> bool:
    return _DENYLIST_RE.match(basename) is not None


def _is_binary_content(section: str) -> bool:
    """Return True if the section looks like binary content.

    A NUL byte or a string that cannot be encoded as UTF-8 (e.g., lone
    surrogates surviving from an upstream decode) is treated as binary.
    """
    if "\x00" in section:
        return True
    try:
        section.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def filter_diff(diff: str, max_eligible_files: int = 50) -> EligibleDiff:
    """Filter a unified diff by denylist and binary detection.

    Never raises. On empty or non-diff input, returns an empty ``EligibleDiff``.
    ``too_large`` reflects the count of *kept* files strictly exceeding
    ``max_eligible_files``; the content is not truncated when too large.
    """
    if not diff:
        return _EMPTY

    matches = list(_HEADER_RE.finditer(diff))
    if not matches:
        return _EMPTY

    kept_sections: list[str] = []
    excluded: list[str] = []
    total_files = len(matches)

    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(diff)
        section = diff[start:end]
        basename = _basename(_unescape_git_path(match.group(2)))

        if _is_denylisted(basename):
            excluded.append(basename)
            continue
        if _is_binary_content(section):
            excluded.append(basename)
            continue
        kept_sections.append(section)

    content = "".join(kept_sections)
    excluded_sorted = tuple(sorted(excluded))
    kept_count = len(kept_sections)

    return EligibleDiff(
        content=content,
        excluded_files=excluded_sorted,
        excluded_count=len(excluded_sorted),
        total_files=total_files,
        is_empty=(kept_count == 0),
        too_large=(kept_count > max_eligible_files),
    )
