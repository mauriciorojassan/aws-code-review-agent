"""Lambda handler — synchronous PR review pipeline orchestration.

Wires the Wave 1-3 modules into the 16-step flow documented in
``design.md`` §1. The handler owns exit-code selection, structured logging
at every terminal state, and translation between GitHub webhook events
and the internal domain (:class:`WebhookPayload`, :class:`Finding`).

Timeout budget (30-second Lambda cap):

============================ ============ ================================
Step                          Nominal ms   Retry / notes
============================ ============ ================================
Signature + filters + parse   <100         none
S3 analysis cache lookup      ~200         none
Diff fetch                    <2000        1 retry on 5xx / transport
S3 diff put                   ~200         best-effort (fail-silent)
Bedrock invoke                up to 25000  no retry (max_attempts=1)
Findings + S3 analysis put    <500         none / best-effort
Dedup GET + review POST       <2000        1 retry inside publisher
Neutral comment fallback      <500         no retry (best-effort)
============================ ============ ================================

Worst-case realistic path (diff-fetch retry AND Bedrock at ceiling) is
~35 s. Lambda kills the container at 30 s → HTTP 5xx → GitHub retries
the webhook delivery. Accepted trade-off; documented in ``design.md``.

HTTP exit matrix:

===== ========================================================================
Code  When
===== ========================================================================
200   Successful review post, dedup hit, filtered event/action, empty PR,
      too-large PR, out-of-hunk findings, rate-limit fallback.
400   Payload JSON malformed or fails Pydantic validation.
401   Webhook signature missing / invalid.
500   Bedrock raised ClientError / BotoCoreError.
502   Diff fetch failed after retry, or review POST failed with non-rate-
      limit github_error after retry.
===== ========================================================================
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError

from code_review_agent import (
    credentials,
    diff_cache,
    diff_filter,
    diff_parser,
    github_client,
    observability,
    review_publisher,
    reviewer,
    webhook_validator,
)
from code_review_agent.models import Finding, WebhookPayload

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_EVENT_LOG_NAME = "pr_review"
_MAX_ELIGIBLE_FILES = 50  # mirrors diff_filter default; used only in messages


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Process one GitHub webhook delivery end-to-end.

    Returns an API Gateway HTTP API v2 response dict. See module docstring
    for the exit-code matrix and ``design.md`` §1 for the full contract.
    """
    body_bytes = _get_body_bytes(event)
    headers = _lowercase_headers(event)

    # Step 1: signature validation. Runs before JSON parse so a malformed
    # body still yields a proper 401 rather than a 400.
    secret = credentials.get_webhook_secret()
    signature = headers.get("x-hub-signature-256")
    if not webhook_validator.validate_signature(secret, body_bytes, signature):
        logger.warning("Rejected: invalid webhook signature")
        return _response(401, {"message": "invalid signature"})

    # Step 2: event-header filter.
    should_process, event_reason = webhook_validator.filter_event(headers.get("x-github-event"))
    if not should_process:
        logger.info("Filtered event: %s", event_reason)
        return _response(200, {"message": f"ignored: {event_reason}"})

    # Step 3: parse payload.
    try:
        payload_dict = json.loads(body_bytes)
        payload = WebhookPayload(**payload_dict)
    except (json.JSONDecodeError, ValidationError, TypeError) as e:
        logger.warning("Rejected: malformed payload: %s", e)
        return _response(400, {"message": "malformed payload"})

    # Step 4: action filter.
    should_process, action_reason = webhook_validator.filter_action(payload.action)
    if not should_process:
        logger.info("Filtered action: %s", action_reason)
        return _response(200, {"message": f"ignored: {action_reason}"})

    repo = payload.repository.full_name
    pr = payload.pull_request.number
    head_sha = payload.pull_request.head.sha
    pr_url = f"https://github.com/{repo}/pull/{pr}"
    action = payload.action
    token = credentials.get_github_token()

    # Step 5: analysis-cache hit → skip fetch + filter + Bedrock + hunk
    # validation. Publish (with dedup check) still runs — cache-hit + dedup
    # is the idempotent double-delivery path.
    cached_findings = diff_cache.get_cached_analysis(repo, pr, head_sha)
    if cached_findings is not None:
        logger.info("Analysis cache hit for %s#%d @ %s", repo, pr, head_sha)
        return _publish_and_return(
            repo=repo,
            pr=pr,
            head_sha=head_sha,
            pr_url=pr_url,
            action=action,
            findings=cached_findings,
            excluded_count=0,  # not preserved across cache boundary in v1
            token=token,
            cached=True,
        )

    # Step 6: fetch diff (with the client's built-in retry).
    try:
        diff = github_client.fetch_pr_diff(repo, pr, token=token)
    except github_client.RateLimitError:
        return _handle_rate_limit(
            repo=repo,
            pr=pr,
            pr_url=pr_url,
            head_sha=head_sha,
            action=action,
            token=token,
            source="diff_fetch",
        )
    except github_client.GitHubFetchError as e:
        logger.error("Diff fetch failed for %s#%d: %s", repo, pr, e)
        _emit_failure(
            repo=repo,
            pr_url=pr_url,
            action=action,
            head_sha=head_sha,
            reason="diff_fetch_error",
        )
        return _response(502, {"message": "diff fetch failed"})

    # Step 7: cache the complete diff (best-effort — errors are swallowed
    # inside diff_cache.put_diff).
    diff_cache.put_diff(repo, pr, head_sha, diff)

    # Step 8: eligibility filter + reviewability gate.
    eligible = diff_filter.filter_diff(diff)
    if eligible.is_empty:
        return _post_neutral_and_return(
            repo=repo,
            pr=pr,
            head_sha=head_sha,
            pr_url=pr_url,
            action=action,
            token=token,
            body="No changes to review.",
            reason="no_changes",
        )
    if eligible.too_large:
        kept = eligible.total_files - eligible.excluded_count
        body = (
            f"This PR has {kept} eligible file(s) after filtering, more than "
            f"the {_MAX_ELIGIBLE_FILES}-file automated review limit. Skipping."
        )
        return _post_neutral_and_return(
            repo=repo,
            pr=pr,
            head_sha=head_sha,
            pr_url=pr_url,
            action=action,
            token=token,
            body=body,
            reason="too_large",
        )

    # Step 9: Bedrock analysis.
    try:
        raw_findings = reviewer.analyze_diff(eligible.content)
    except (ClientError, BotoCoreError) as e:
        logger.error("Bedrock analysis failed for %s#%d: %s", repo, pr, e)
        _emit_failure(
            repo=repo,
            pr_url=pr_url,
            action=action,
            head_sha=head_sha,
            reason="bedrock_error",
        )
        return _response(500, {"message": "bedrock analysis failed"})

    # Step 10-11: validate findings against parsed hunk map. `line >= 1`
    # is already enforced at the model layer (Finding.line = Field(gt=0))
    # so no explicit line<1 filter is needed here.
    hunk_map = diff_parser.parse_unified_diff(eligible.content)
    valid_findings: list[Finding] = []
    out_of_hunk_count = 0
    for finding in raw_findings:
        if diff_parser.validate_finding(finding, hunk_map):
            valid_findings.append(finding)
        else:
            out_of_hunk_count += 1

    if out_of_hunk_count > 0:
        logger.warning(
            "Out-of-hunk findings for %s#%d: %d skipped",
            repo,
            pr,
            out_of_hunk_count,
        )
        _emit_failure(
            repo=repo,
            pr_url=pr_url,
            action=action,
            head_sha=head_sha,
            reason="out_of_hunk",
            out_of_hunk_count=out_of_hunk_count,
        )
        return _response(200, {"message": "out-of-hunk findings, review skipped"})

    # Step 12: cache the validated findings (best-effort).
    diff_cache.put_analysis(repo, pr, head_sha, valid_findings)

    # Steps 13-14: publish (dedup check, sort, 20-cap + overflow, rate-limit,
    # retry all live inside publish_review).
    return _publish_and_return(
        repo=repo,
        pr=pr,
        head_sha=head_sha,
        pr_url=pr_url,
        action=action,
        findings=valid_findings,
        excluded_count=eligible.excluded_count,
        token=token,
        cached=False,
    )


# ---------------------------------------------------------------------------
# Terminal-state helpers
# ---------------------------------------------------------------------------


def _publish_and_return(
    *,
    repo: str,
    pr: int,
    head_sha: str,
    pr_url: str,
    action: str,
    findings: list[Finding],
    excluded_count: int,
    token: str | None,
    cached: bool,
) -> dict[str, Any]:
    """Post the review and translate the outcome to an HTTP response + metrics."""
    summary = {"excluded_files": excluded_count, "truncated": False}
    result = review_publisher.publish_review(repo, pr, head_sha, findings, summary, token=token)
    severity_counts = _severity_counts(findings)

    if result.success:
        observability.emit_metric(
            "ReviewCompleted",
            1,
            {"repo": repo, "severity_count": str(len(findings))},
        )
        observability.emit_structured_log(
            _EVENT_LOG_NAME,
            pr_url,
            repo,
            action,
            head_sha,
            result.review_id,
            "success" if result.skipped_reason is None else "skipped",
            skipped_reason=result.skipped_reason,
            cached=cached,
            severity_counts=severity_counts,
            excluded_files=excluded_count,
        )
        message = "review posted" if result.review_id else "review skipped (dedup)"
        return _response(200, {"message": message, "review_id": result.review_id})

    if result.skipped_reason == "rate_limit":
        return _handle_rate_limit(
            repo=repo,
            pr=pr,
            pr_url=pr_url,
            head_sha=head_sha,
            action=action,
            token=token,
            source="review_post",
        )

    # github_error path.
    _emit_failure(
        repo=repo,
        pr_url=pr_url,
        action=action,
        head_sha=head_sha,
        reason="github_error",
    )
    return _response(502, {"message": "github error during publish"})


def _handle_rate_limit(
    *,
    repo: str,
    pr: int,
    pr_url: str,
    head_sha: str,
    action: str,
    token: str | None,
    source: str,
) -> dict[str, Any]:
    """Rate-limit fallback: neutral comment (best-effort), ``ReviewFailed`` metric, 200."""
    review_publisher.post_issue_comment(
        repo,
        pr,
        (
            "⚠️ GitHub API rate limit exhausted while attempting to review "
            "this PR. The reviewer will not retry automatically."
        ),
        token=token,
    )
    observability.emit_metric("ReviewFailed", 1, {"repo": repo, "reason": "rate_limit"})
    observability.emit_structured_log(
        _EVENT_LOG_NAME,
        pr_url,
        repo,
        action,
        head_sha,
        None,
        "failed",
        reason="rate_limit",
        source=source,
    )
    return _response(200, {"message": "rate-limited"})


def _post_neutral_and_return(
    *,
    repo: str,
    pr: int,
    head_sha: str,
    pr_url: str,
    action: str,
    token: str | None,
    body: str,
    reason: str,
) -> dict[str, Any]:
    """Neutral comment for empty / too-large paths; ``ReviewCompleted`` metric; 200."""
    review_publisher.post_issue_comment(repo, pr, body, token=token)
    observability.emit_metric("ReviewCompleted", 1, {"repo": repo, "severity_count": "0"})
    observability.emit_structured_log(
        _EVENT_LOG_NAME,
        pr_url,
        repo,
        action,
        head_sha,
        None,
        "skipped",
        reason=reason,
    )
    return _response(200, {"message": reason})


def _emit_failure(
    *,
    repo: str,
    pr_url: str,
    action: str,
    head_sha: str,
    reason: str,
    **extra: Any,
) -> None:
    """Emit ``ReviewFailed`` metric + structured failure log."""
    observability.emit_metric("ReviewFailed", 1, {"repo": repo, "reason": reason})
    observability.emit_structured_log(
        _EVENT_LOG_NAME,
        pr_url,
        repo,
        action,
        head_sha,
        None,
        "failed",
        reason=reason,
        **extra,
    )


def _severity_counts(findings: list[Finding]) -> dict[str, int]:
    return {
        "error": sum(1 for f in findings if f.severity == "error"),
        "warning": sum(1 for f in findings if f.severity == "warning"),
        "info": sum(1 for f in findings if f.severity == "info"),
    }


# ---------------------------------------------------------------------------
# Event / response helpers
# ---------------------------------------------------------------------------


def _get_body_bytes(event: dict[str, Any]) -> bytes:
    """Extract the raw request body as bytes, honoring ``isBase64Encoded``."""
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(raw)
    return raw.encode("utf-8") if isinstance(raw, str) else b""


def _lowercase_headers(event: dict[str, Any]) -> dict[str, str]:
    """Return headers with lowercased keys.

    API Gateway HTTP API v2 already lowercases, but not every test shim
    or dev proxy does; defensive lowercase keeps the handler portable.
    """
    return {k.lower(): v for k, v in (event.get("headers") or {}).items()}


def _response(status: int, body: dict[str, Any] | str = "") -> dict[str, Any]:
    """Build a uniform API Gateway HTTP API v2 response."""
    body_str = body if isinstance(body, str) else json.dumps(body)
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": body_str,
    }
