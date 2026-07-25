"""GitHub REST API client — fetch PR diffs.

Companion to :mod:`review_publisher`. Together they own all GitHub-facing
HTTP for the Lambda pipeline: this module reads (diff fetch), the
publisher writes (review post + issue comment fallback).

Rate-limit detection is duplicated here rather than shared with
:mod:`review_publisher` on purpose — the two modules can evolve their
handling of edge cases independently, and the 8-line helper is not worth
the coupling of a shared "gh_common" module.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"


class GitHubFetchError(Exception):
    """Raised when a GitHub REST fetch fails after its retry budget."""


class RateLimitError(GitHubFetchError):
    """Raised when GitHub rate-limit exhaustion is detected.

    Subclass of :class:`GitHubFetchError` so callers that don't care to
    distinguish rate limits from other failures can catch the parent.
    """


def fetch_pr_diff(
    repo: str,
    pr: int,
    *,
    client: httpx.Client | None = None,
    token: str | None = None,
) -> str:
    """Fetch the unified diff for a pull request.

    Args:
        repo: ``owner/name`` slug (e.g. ``"octocat/Hello-World"``).
        pr: Pull request number.
        client: Optional :class:`httpx.Client` for dependency injection —
            tests pass an :class:`httpx.MockTransport`-backed client here.
            When ``None``, a short-lived client is created and closed.
        token: Optional GitHub App / PAT token override. When ``None``,
            resolved from ``GITHUB_TOKEN`` at call time. When neither is
            set, the request is sent without an ``Authorization`` header.

    Returns:
        The unified diff content as a string.

    Raises:
        RateLimitError: On GitHub rate-limit exhaustion.
        GitHubFetchError: On persistent transport, 5xx, or non-rate-limit
            4xx failure. 4xx errors (404, 403 non-rate-limit) are raised
            on the first attempt without retry; 5xx and transport errors
            get exactly one retry.
    """
    close_client = False
    if client is None:
        client = httpx.Client(timeout=10.0)
        close_client = True

    try:
        auth = token if token is not None else os.environ.get("GITHUB_TOKEN", "")
        headers = {
            "Accept": "application/vnd.github.v3.diff",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if auth:
            headers["Authorization"] = f"Bearer {auth}"

        url = f"{_GITHUB_API}/repos/{repo}/pulls/{pr}"
        return _get_with_retry(client, url, headers)
    finally:
        if close_client:
            client.close()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_with_retry(
    client: httpx.Client,
    url: str,
    headers: dict[str, str],
) -> str:
    """GET with exactly one retry on transport error or 5xx.

    Behavior matrix:
      * Rate-limit signal → :class:`RateLimitError` immediately, no retry.
      * 5xx → retry once, then :class:`GitHubFetchError`.
      * Transport error (:class:`httpx.HTTPError`) → retry once, then
        :class:`GitHubFetchError`.
      * Non-rate-limit 4xx (404, 403 without rate-limit signal) →
        :class:`GitHubFetchError` immediately. These are deterministic;
        retrying wastes budget.
      * 2xx / 3xx → return response text.
    """
    for attempt in (1, 2):
        try:
            response = client.get(url, headers=headers)
        except httpx.HTTPError as e:
            logger.warning("GitHub GET attempt %d transport error: %s", attempt, e)
            if attempt == 2:
                raise GitHubFetchError(f"transport failure fetching {url}: {e}") from e
            continue

        if _is_rate_limited(response):
            logger.warning("GitHub rate limit exhausted on attempt %d", attempt)
            raise RateLimitError(f"rate limit exhausted at {url}")

        if response.status_code < 400:
            return response.text

        if response.status_code >= 500:
            logger.warning(
                "GitHub GET attempt %d returned %s: %s",
                attempt,
                response.status_code,
                response.text[:200],
            )
            if attempt == 2:
                raise GitHubFetchError(f"server error {response.status_code} fetching {url}")
            continue

        # Non-rate-limit 4xx — deterministic, no retry.
        raise GitHubFetchError(
            f"client error {response.status_code} fetching {url}: " f"{response.text[:200]}"
        )

    # Unreachable — the loop either returns or raises on every path.
    raise GitHubFetchError(f"unreachable retry-loop exit fetching {url}")  # pragma: no cover


def _is_rate_limited(response: httpx.Response) -> bool:
    """Detect GitHub rate-limit exhaustion, primary and secondary.

    Same semantics as :func:`review_publisher._is_rate_limited`:

    * Primary hourly limit: 403 with ``X-RateLimit-Remaining: 0``.
    * Secondary abuse limit: 403 or 429 with ``Retry-After``.
    * Bare 429 (RFC 6585) is unconditionally rate-limit.
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
