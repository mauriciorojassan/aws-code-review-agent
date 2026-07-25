"""Parse unified diff hunks and validate findings against post-state file lines."""

from __future__ import annotations

import re
from dataclasses import dataclass

from code_review_agent.diff_filter import _HEADER_RE, _unescape_git_path
from code_review_agent.models import Finding

# Matches a unified-diff hunk header on its own line, supporting both the short
# (`@@ -a +c @@`) and long (`@@ -a,b +c,d @@`) forms. Trailing function-context
# text after the closing `@@` is intentionally ignored.
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class HunkRange:
    """A contiguous range of post-state (new-file) line numbers touched by a hunk."""

    start_line: int  # absolute line in new (post-state) file
    line_count: int  # number of lines covered by this hunk (>= 1)


def parse_unified_diff(diff: str) -> dict[str, list[HunkRange]]:
    """Parse a unified diff into a map of file paths to their post-state hunk ranges.

    Returns an empty dict on empty or non-diff input; never raises. Files with no
    valid post-state hunks (delete-only sections, renames without content changes,
    binary sections, etc.) are omitted from the map. Hunk ranges are returned in
    document order — not sorted.
    """
    if not diff:
        return {}

    matches = list(_HEADER_RE.finditer(diff))
    if not matches:
        return {}

    hunk_map: dict[str, list[HunkRange]] = {}

    for index, header in enumerate(matches):
        start = header.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(diff)
        section = diff[start:end]

        file_path = _unescape_git_path(header.group(2))

        hunks: list[HunkRange] = []
        for hunk_match in _HUNK_RE.finditer(section):
            start_line = int(hunk_match.group(1))
            line_count = int(hunk_match.group(2)) if hunk_match.group(2) is not None else 1
            # Skip delete-only hunks (start_line == 0) and empty hunks (line_count == 0).
            # Neither contributes a post-state line range that a finding can reference.
            if start_line == 0 or line_count == 0:
                continue
            hunks.append(HunkRange(start_line=start_line, line_count=line_count))

        if hunks:
            hunk_map[file_path] = hunks

    return hunk_map


def validate_finding(finding: Finding, hunk_map: dict[str, list[HunkRange]]) -> bool:
    """Return True iff ``finding.file`` is present in ``hunk_map`` AND
    ``finding.line`` falls within at least one of its post-state ``HunkRange``s.

    Never raises. Returns False when the file is not in the map or when the
    line falls outside every hunk. ``finding.line >= 1`` is guaranteed by
    the :class:`Finding` model (``Field(gt=0)``); no runtime check needed
    here.
    """
    if finding.file not in hunk_map:
        return False
    for hunk in hunk_map[finding.file]:
        end_line = hunk.start_line + hunk.line_count - 1
        if hunk.start_line <= finding.line <= end_line:
            return True
    return False
