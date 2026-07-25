"""Publish GitHub PR reviews with inline comments, dedup marker, and retry.

This module owns the last step of the pipeline described in ``design.md`` §7:
turn a validated list of :class:`Finding` objects into a single GitHub PR
review that is safe to submit, deterministic in ordering, and idempotent
across duplicate webhook deliveries for the same head SHA.

Key invariants:
  * At most **20** inline comments per review. Extras spill into a fenced
    code block inside the review body, ordered by the same priority key.
  * A dedup marker ``<!-- cra-dedup: {head_sha} -->`` is embedded in every
    review body; before posting, existing bot reviews for the PR are
    checked for the same marker and the post is skipped when a match is
    found.
  * HTTP 403 with ``X-RateLimit-Remaining: 0`` short-circuits without a
    retry; every *other* transport / server error is retried exactly once.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx

from code_review_agent.models import Finding

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
_MAX_INLINE = 20

# Lower value = higher priority when deciding which findings survive the
# 20-inline cap and how the overflow block is rendered.
_SEVERITY_ORDER: dict[str, int] = {"error": 0, "warning": 1, "info": 2}


@dataclass(frozen=True)
class PublishResult:
    """Outcome of a ``publish_review`` call.

    Attributes:
        success: ``True`` when the review was posted OR the operation was
            deliberately skipped for dedup (an idempotent no-op is still a
            success from the caller's perspective).
        review_id: GitHub review id as a string when a review was actually
            created; ``None`` on dedup skip or failure.
        skipped_reason: One of ``"dedup"``, ``"rate_limit"``,
            ``"github_error"``, or ``None`` when the review was posted.
    """

    success: bool
    review_id: str | None = None
    skipped_reason: str | None = None


def publish_review(
    repo: str,
    pr: int,
    head_sha: str,
    findings: list[Finding],
    summary_data: dict[str, Any],
    *,
    client: httpx.Client | None = None,
    token: str | None = None,
) -> PublishResult:
    """Publish a single GitHub PR review from a list of validated findings.

    Args:
        repo: ``owner/name`` slug.
        pr: Pull request number.
        head_sha: Commit SHA of the PR head; embedded in the dedup marker
            and sent as ``commit_id`` to GitHub.
        findings: Validated findings. May be empty (a summary-only review
            is still posted per US-4).
        summary_data: Free-form dict consumed by :func:`_build_review_body`.
            Recognized keys: ``excluded_files`` (int), ``truncated`` (bool),
            ``truncation_note`` (str).
        client: Optional :class:`httpx.Client` for dependency injection —
            tests pass an :class:`httpx.MockTransport`-backed client here.
            When ``None``, a short-lived client is created and closed.
        token: Optional GitHub App / PAT token override. When ``None``,
            resolved from ``GITHUB_TOKEN`` at call time; when neither is
            set, the request is sent without an ``Authorization`` header.

    Returns:
        A :class:`PublishResult` describing the outcome.
    """
    close_client = False
    if client is None:
        client = httpx.Client(timeout=10.0)
        close_client = True

    try:
        auth = token if token is not None else os.environ.get("GITHUB_TOKEN", "")
        headers = _github_headers(auth)

        if _has_existing_review(client, repo, pr, head_sha, headers):
            logger.info("Dedup hit: skipping review for %s#%d @ %s", repo, pr, head_sha)
            return PublishResult(success=True, review_id=None, skipped_reason="dedup")

        sorted_findings = _sort_findings(findings)
        inline = sorted_findings[:_MAX_INLINE]
        overflow = sorted_findings[_MAX_INLINE:]

        payload: dict[str, Any] = {
            "commit_id": head_sha,
            "event": "COMMENT",
            "body": _build_review_body(sorted_findings, overflow, summary_data, head_sha),
            "comments": [_finding_to_comment(f) for f in inline],
        }

        url = f"{_GITHUB_API}/repos/{repo}/pulls/{pr}/reviews"
        return _post_with_retry(client, url, payload, headers)
    finally:
        if close_client:
            client.close()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _github_headers(token: str) -> dict[str, str]:
    """Return baseline GitHub API headers, adding auth only when a token exists."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _has_existing_review(
    client: httpx.Client,
    repo: str,
    pr: int,
    head_sha: str,
    headers: dict[str, str],
) -> bool:
    """Return ``True`` iff a prior bot review for this head SHA exists.

    A dedup-check failure (network error, non-200, non-JSON) is treated as
    "unknown → proceed with post". We prefer occasional duplicate reviews
    to silently swallowing every publication when the check endpoint is
    flaky.
    """
    url = f"{_GITHUB_API}/repos/{repo}/pulls/{pr}/reviews"
    marker = _dedup_marker(head_sha)
    try:
        response = client.get(url, headers=headers)
    except httpx.HTTPError as e:
        logger.warning("Dedup check transport error, proceeding with post: %s", e)
        return False

    if response.status_code != 200:
        logger.warning("Dedup check returned %s, proceeding with post", response.status_code)
        return False

    try:
        reviews = response.json()
    except ValueError as e:
        logger.warning("Dedup check returned non-JSON, proceeding with post: %s", e)
        return False

    if not isinstance(reviews, list):
        return False
    for review in reviews:
        if not isinstance(review, dict):
            continue
        body = review.get("body")
        if isinstance(body, str) and marker in body:
            return True
    return False


def _post_with_retry(
    client: httpx.Client,
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
) -> PublishResult:
    """POST the review with exactly one retry on non-rate-limit failure.

    Rate-limit exhaustion (403 + ``X-RateLimit-Remaining: 0``) is terminal
    on the *first* observation: retrying against an exhausted quota only
    burns time and yields the same result.
    """
    last_status: int | None = None
    for attempt in (1, 2):
        try:
            response = client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as e:
            logger.warning("GitHub POST attempt %d transport error: %s", attempt, e)
            if attempt == 2:
                return PublishResult(success=False, skipped_reason="github_error")
            continue

        if _is_rate_limited(response):
            logger.warning("GitHub rate limit exhausted on attempt %d", attempt)
            return PublishResult(success=False, skipped_reason="rate_limit")

        if response.status_code < 400:
            review_id = _extract_review_id(response)
            return PublishResult(success=True, review_id=review_id)

        last_status = response.status_code
        logger.warning(
            "GitHub POST attempt %d returned %s: %s",
            attempt,
            response.status_code,
            response.text[:200],
        )

    logger.warning("GitHub POST failed after retry (last status=%s)", last_status)
    return PublishResult(success=False, skipped_reason="github_error")


def _is_rate_limited(response: httpx.Response) -> bool:
    """Detect GitHub rate-limit exhaustion, primary and secondary.

    GitHub returns rate-limit signals on **either** status 403 or 429:

    * Primary hourly limit: 403 with ``X-RateLimit-Remaining: 0``.
    * Secondary abuse limit: 403 or 429 with a ``Retry-After`` header.
    * 429 is defined as a rate-limit status by RFC 6585; even without a
      GitHub-specific header we treat it as one.

    Any 403 without one of those signals is a permissions / policy
    failure — *not* a rate limit — and falls through to the generic
    retry-once branch of :func:`_post_with_retry`.
    """
    status = response.status_code
    if status == 429:
        return True
    if status == 403:
        if response.headers.get("X-RateLimit-Remaining") == "0":
            return True
        if response.headers.get("Retry-After"):
            return True
    return False


def _extract_review_id(response: httpx.Response) -> str | None:
    """Extract the review id from a successful POST response, tolerating shape drift."""
    try:
        data = response.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    review_id = data.get("id")
    if review_id is None:
        return None
    return str(review_id)


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _dedup_marker(head_sha: str) -> str:
    """Return the HTML-comment dedup marker for a given head SHA."""
    return f"<!-- cra-dedup: {head_sha} -->"


def _sort_findings(findings: list[Finding]) -> list[Finding]:
    """Deterministic sort: severity priority → file path → line number."""
    return sorted(
        findings,
        key=lambda f: (_SEVERITY_ORDER.get(f.severity, 99), f.file, f.line),
    )


def _finding_to_comment(finding: Finding) -> dict[str, Any]:
    """Render one finding as a GitHub review-comment object.

    ``side: "RIGHT"`` anchors the comment to the new-file line, matching the
    validator invariant established in ``design.md`` §5.

    LLM-generated ``message`` and ``suggestion`` are passed through
    :func:`_escape_markdown` at render time so backticks and leading pipes
    can't break out of the rendering context (see F8, Wave 4 JD).
    """
    message = _escape_markdown(finding.message)
    header = f"**[{finding.severity.upper()}]** {message}"
    if finding.suggestion:
        suggestion = _escape_markdown(finding.suggestion)
        body = f"{header}\n\n💡 {suggestion}"
    else:
        body = header
    return {
        "path": finding.file,
        "line": finding.line,
        "side": "RIGHT",
        "body": body,
    }


def _build_review_body(
    all_findings: list[Finding],
    overflow: list[Finding],
    summary_data: dict[str, Any],
    head_sha: str,
) -> str:
    """Assemble the Markdown review body.

    Layout:
      1. Header + severity-counts table (always).
      2. Excluded-file note (when count > 0).
      3. Truncation note (when ``summary_data['truncated']`` is truthy).
      4. "No actionable findings" line (when ``all_findings`` is empty).
      5. Overflow block (when >20 findings).
      6. Dedup marker (always, last line).
    """
    errors = sum(1 for f in all_findings if f.severity == "error")
    warnings = sum(1 for f in all_findings if f.severity == "warning")
    infos = sum(1 for f in all_findings if f.severity == "info")

    excluded = _coerce_nonneg_int(summary_data.get("excluded_files"))
    truncated = bool(summary_data.get("truncated", False))

    parts: list[str] = [
        "## 🤖 Code Review Agent",
        "",
        "| Severity | Count |",
        "|----------|-------|",
        f"| 🔴 Error | {errors} |",
        f"| 🟡 Warning | {warnings} |",
        f"| 🔵 Info | {infos} |",
        "",
    ]

    if excluded > 0:
        parts.append(
            f"Excluded **{excluded}** file(s) from analysis (lockfiles, minified, binary)."
        )
        parts.append("")

    if truncated:
        note = summary_data.get("truncation_note") or (
            "Diff was truncated at 100 eligible hunks; some changes were not reviewed."
        )
        parts.append(f"> ⚠️ {note}")
        parts.append("")

    if not all_findings:
        parts.append("No actionable findings detected.")
        parts.append("")

    if overflow:
        parts.append(f"### Additional findings ({len(overflow)})")
        parts.append("")
        parts.append("```")
        for finding in overflow:
            message = _escape_markdown(finding.message)
            suffix = f" — {_escape_markdown(finding.suggestion)}" if finding.suggestion else ""
            parts.append(
                f"[{finding.severity.upper()}] {finding.file}:{finding.line} — "
                f"{message}{suffix}"
            )
        parts.append("```")
        parts.append("")

    parts.append(_dedup_marker(head_sha))
    return "\n".join(parts)


def _coerce_nonneg_int(value: Any) -> int:
    """Best-effort coerce ``value`` to a non-negative int; 0 on failure."""
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return max(result, 0)


# Compiled once at import; ``re.sub`` is called per finding so keeping the
# pattern module-level saves the compile cost on every render.
_LEADING_PIPE_RE = re.compile(r"(^|\n)[ \t]*\|")


def _escape_markdown(text: str) -> str:
    """Strip characters that could break out of our rendering context.

    Two LLM-content risks addressed at render time (F8, Wave 4 JD):

    1. **Triple-backticks**. Overflow findings are rendered inside a fenced
       code block. A triple-backtick sequence in a finding message that
       landed at the start of a line would close the fence prematurely
       and leak subsequent content out of the code context. Stripped
       entirely — simpler than variable-width fence detection, and
       lossless for the reader (backtick clusters are rare in review
       prose).

    2. **Leading pipes**. A pipe at the start of a line can be interpreted
       as a table-row delimiter. GitHub markdown *requires* a separator
       row (``|---|``) before a real table renders, so the risk is small
       — but stripping keeps rendering deterministic against future
       renderer changes and against LLM output that happens to include a
       markdown-like separator pattern.

    Empty input is returned as-is (no allocation).
    """
    if not text:
        return text
    text = text.replace("```", "")
    text = _LEADING_PIPE_RE.sub(r"\1", text)
    return text


# ---------------------------------------------------------------------------
# Issue-comment fallback (neutral comments — too large / no changes / rate limit)
# ---------------------------------------------------------------------------


def post_issue_comment(
    repo: str,
    pr: int,
    body: str,
    *,
    client: httpx.Client | None = None,
    token: str | None = None,
) -> bool:
    """Post a neutral issue comment on a PR — best-effort, never raises.

    Used for design flows where a full review isn't appropriate:

      * "PR too large" (>50 eligible files after filtering).
      * "No changes to review" (0 eligible files).
      * "GitHub rate limit exhausted" (rate-limit fallback from
        :func:`publish_review`).

    A single POST attempt is made — no retry. The caller has already
    concluded that a full review is impossible; aggressively retrying a
    fallback path (especially the rate-limit case) would defeat its
    purpose. Any failure is logged and swallowed; the return value is a
    boolean signal for observability, not a raise-on-failure contract.

    Args:
        repo: ``owner/name`` slug.
        pr: Pull request number (used as the issue number — PRs and
            issues share the same numbering space on GitHub).
        body: Comment body (Markdown).
        client: Optional :class:`httpx.Client` for dependency injection.
        token: Optional GitHub App / PAT token override. Falls back to
            ``GITHUB_TOKEN`` env var at call time.

    Returns:
        ``True`` on 2xx / 3xx, ``False`` on any transport error or 4xx/5xx.
    """
    close_client = False
    if client is None:
        client = httpx.Client(timeout=10.0)
        close_client = True

    try:
        auth = token if token is not None else os.environ.get("GITHUB_TOKEN", "")
        headers = _github_headers(auth)
        url = f"{_GITHUB_API}/repos/{repo}/issues/{pr}/comments"

        try:
            response = client.post(url, json={"body": body}, headers=headers)
        except httpx.HTTPError as e:
            logger.warning("Issue-comment POST transport error for %s#%d: %s", repo, pr, e)
            return False

        if response.status_code < 400:
            return True

        logger.warning(
            "Issue-comment POST returned %s for %s#%d: %s",
            response.status_code,
            repo,
            pr,
            response.text[:200],
        )
        return False
    finally:
        if close_client:
            client.close()
