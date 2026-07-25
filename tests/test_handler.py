"""Integration tests for the Lambda handler orchestration.

Strategy:
  * Real (moto-backed) S3 for :mod:`diff_cache` — verifies cache-hit
    branches exercise real key layouts.
  * Real :mod:`webhook_validator`, :mod:`diff_filter`, :mod:`diff_parser`,
    :mod:`models` — they are small, pure, and independently tested; using
    them here catches wiring bugs at zero cost.
  * External I/O modules — :func:`github_client.fetch_pr_diff`,
    :func:`reviewer.analyze_diff`, :func:`review_publisher.publish_review`,
    :func:`review_publisher.post_issue_comment` — patched at the handler's
    namespace via a ``deps`` fixture so each test dictates behavior.
  * Observability — :func:`emit_metric` and :func:`emit_structured_log` are
    patched with :class:`MagicMock` so tests can assert exact call args.

The signed-event helper produces a real HMAC-SHA256 against
``WEBHOOK_SECRET`` so signature-branch tests exercise the actual
validator, not a mock of it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from code_review_agent import handler, review_publisher
from code_review_agent.models import Finding

_SECRET = b"test-webhook-secret"
_REPO = "octocat/Hello-World"
_PR = 42
_SHA = "abc123def456789012345678901234567890abcd"
_OTHER_SHA = "1111111111111111111111111111111111111111"


# ---------------------------------------------------------------------------
# Event / payload builders
# ---------------------------------------------------------------------------


def _sample_payload(
    *,
    action: str = "opened",
    head_sha: str = _SHA,
    repo_full_name: str = _REPO,
    pr_number: int = _PR,
) -> dict[str, Any]:
    owner, name = repo_full_name.split("/", 1)
    return {
        "action": action,
        "repository": {
            "full_name": repo_full_name,
            "name": name,
            "owner": {"login": owner},
        },
        "pull_request": {
            "number": pr_number,
            "title": "Test PR",
            "head": {"sha": head_sha, "ref": "feature/x"},
            "diff_url": f"https://github.com/{repo_full_name}/pull/{pr_number}.diff",
        },
    }


def _signed_event(
    payload: dict[str, Any] | str | bytes,
    *,
    secret: bytes = _SECRET,
    event_type: str = "pull_request",
    tamper_signature: bool = False,
    omit_signature: bool = False,
    base64_body: bool = False,
) -> dict[str, Any]:
    """Build an API Gateway HTTP API v2 event with a real HMAC signature."""
    if isinstance(payload, dict):
        body_str = json.dumps(payload)
    elif isinstance(payload, bytes):
        body_str = payload.decode("utf-8", errors="replace")
    else:
        body_str = payload

    body_bytes = body_str.encode("utf-8")
    digest = hmac.new(secret, body_bytes, hashlib.sha256).hexdigest()
    if tamper_signature:
        digest = "0" * len(digest)
    sig_value = f"sha256={digest}"

    headers: dict[str, str] = {"x-github-event": event_type}
    if not omit_signature:
        headers["x-hub-signature-256"] = sig_value

    event: dict[str, Any] = {"headers": headers}
    if base64_body:
        event["body"] = base64.b64encode(body_bytes).decode("ascii")
        event["isBase64Encoded"] = True
    else:
        event["body"] = body_str
    return event


def _finding(
    file: str = "src/main.py",
    line: int = 3,
    severity: str = "warning",
    message: str = "check this",
) -> Finding:
    return Finding(
        file=file,
        line=line,
        severity=severity,  # type: ignore[arg-type]
        message=message,
    )


# A minimal unified diff that produces exactly one right-side hunk covering
# lines 1..3 of ``src/main.py``. Findings with line in [1,3] will validate.
_VALID_DIFF = (
    "diff --git a/src/main.py b/src/main.py\n"
    "--- a/src/main.py\n"
    "+++ b/src/main.py\n"
    "@@ -1,3 +1,3 @@\n"
    "-old line 1\n"
    "-old line 2\n"
    "-old line 3\n"
    "+new line 1\n"
    "+new line 2\n"
    "+new line 3\n"
)


# ---------------------------------------------------------------------------
# Dependency injection fixture
# ---------------------------------------------------------------------------


@dataclass
class _HandlerDeps:
    """Bundle of mocked handler dependencies; tests configure return values."""

    fetch_pr_diff: MagicMock
    analyze_diff: MagicMock
    publish_review: MagicMock
    post_issue_comment: MagicMock
    emit_metric: MagicMock
    emit_structured_log: MagicMock


@pytest.fixture
def deps(monkeypatch: pytest.MonkeyPatch) -> _HandlerDeps:
    """Patch every external touchpoint the handler calls.

    Each mock has a sensible default so a "happy path" test can construct
    only the deltas it cares about.
    """
    monkeypatch.setenv("WEBHOOK_SECRET", _SECRET.decode("utf-8"))
    monkeypatch.setenv("GITHUB_TOKEN", "test-github-token")

    fetch = MagicMock(name="fetch_pr_diff", return_value=_VALID_DIFF)
    analyze = MagicMock(name="analyze_diff", return_value=[])
    publish = MagicMock(
        name="publish_review",
        return_value=review_publisher.PublishResult(
            success=True, review_id="rev-99", skipped_reason=None
        ),
    )
    post_comment = MagicMock(name="post_issue_comment", return_value=True)
    metric = MagicMock(name="emit_metric")
    log = MagicMock(name="emit_structured_log")

    monkeypatch.setattr(handler.github_client, "fetch_pr_diff", fetch)
    monkeypatch.setattr(handler.reviewer, "analyze_diff", analyze)
    monkeypatch.setattr(handler.review_publisher, "publish_review", publish)
    monkeypatch.setattr(handler.review_publisher, "post_issue_comment", post_comment)
    monkeypatch.setattr(handler.observability, "emit_metric", metric)
    monkeypatch.setattr(handler.observability, "emit_structured_log", log)

    return _HandlerDeps(fetch, analyze, publish, post_comment, metric, log)


# ---------------------------------------------------------------------------
# Scenario 1 — valid webhook → analysis → review post
# ---------------------------------------------------------------------------


def test_valid_webhook_end_to_end_posts_review(s3_bucket: Any, deps: _HandlerDeps) -> None:
    deps.analyze_diff.return_value = [
        _finding(line=1, severity="error"),
        _finding(line=2, severity="warning"),
        _finding(line=3, severity="info"),
    ]

    event = _signed_event(_sample_payload())
    response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["review_id"] == "rev-99"

    deps.fetch_pr_diff.assert_called_once_with(
        _REPO,
        _PR,
        token="test-github-token",  # noqa: S106 -- test literal, set by fixture
    )
    deps.analyze_diff.assert_called_once()
    deps.publish_review.assert_called_once()
    call = deps.publish_review.call_args
    assert call.args[0] == _REPO
    assert call.args[1] == _PR
    assert call.args[2] == _SHA
    assert len(call.args[3]) == 3  # findings

    deps.emit_metric.assert_called_with(
        "ReviewCompleted", 1, {"repo": _REPO, "severity_count": "3"}
    )
    log_call = deps.emit_structured_log.call_args
    assert log_call.args[0] == "pr_review"
    assert log_call.args[6] == "success"
    assert log_call.kwargs["cached"] is False
    assert log_call.kwargs["severity_counts"] == {"error": 1, "warning": 1, "info": 1}


# ---------------------------------------------------------------------------
# Scenario 2 — filtered event → 200 no-op
# ---------------------------------------------------------------------------


def test_non_pull_request_event_returns_200_noop(s3_bucket: Any, deps: _HandlerDeps) -> None:
    event = _signed_event(_sample_payload(), event_type="push")
    response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 200
    assert "ignored" in json.loads(response["body"])["message"]
    deps.fetch_pr_diff.assert_not_called()
    deps.analyze_diff.assert_not_called()
    deps.publish_review.assert_not_called()


def test_filtered_action_returns_200_noop(s3_bucket: Any, deps: _HandlerDeps) -> None:
    event = _signed_event(_sample_payload(action="closed"))
    response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 200
    assert "ignored" in json.loads(response["body"])["message"]
    deps.fetch_pr_diff.assert_not_called()


def test_missing_event_header_returns_200_noop(s3_bucket: Any, deps: _HandlerDeps) -> None:
    event = _signed_event(_sample_payload())
    event["headers"].pop("x-github-event")
    response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 200


# ---------------------------------------------------------------------------
# Scenario 3 — invalid signature → 401
# ---------------------------------------------------------------------------


def test_invalid_signature_returns_401(s3_bucket: Any, deps: _HandlerDeps) -> None:
    event = _signed_event(_sample_payload(), tamper_signature=True)
    response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 401
    deps.fetch_pr_diff.assert_not_called()
    deps.publish_review.assert_not_called()


def test_missing_signature_returns_401(s3_bucket: Any, deps: _HandlerDeps) -> None:
    event = _signed_event(_sample_payload(), omit_signature=True)
    response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 401


def test_signature_computed_over_base64_decoded_body(s3_bucket: Any, deps: _HandlerDeps) -> None:
    """The HMAC compares against the *decoded* body, not the base64 string."""
    event = _signed_event(_sample_payload(), base64_body=True)
    assert event["isBase64Encoded"] is True
    response = handler.lambda_handler(event, None)
    assert response["statusCode"] == 200


# ---------------------------------------------------------------------------
# Scenario 4 — malformed payload → 400
# ---------------------------------------------------------------------------


def test_malformed_json_body_returns_400(s3_bucket: Any, deps: _HandlerDeps) -> None:
    event = _signed_event("not { json")
    response = handler.lambda_handler(event, None)
    assert response["statusCode"] == 400


def test_payload_missing_required_fields_returns_400(s3_bucket: Any, deps: _HandlerDeps) -> None:
    incomplete = {"action": "opened", "repository": {"full_name": "x/y"}}
    event = _signed_event(incomplete)
    response = handler.lambda_handler(event, None)
    assert response["statusCode"] == 400


# ---------------------------------------------------------------------------
# Scenario 5 — PR too large → 200 + neutral comment
# ---------------------------------------------------------------------------


def _build_multi_file_diff(count: int) -> str:
    parts = []
    for i in range(count):
        parts.append(
            f"diff --git a/f{i:03d}.py b/f{i:03d}.py\n"
            f"--- a/f{i:03d}.py\n"
            f"+++ b/f{i:03d}.py\n"
            f"@@ -1 +1 @@\n"
            f"-old\n"
            f"+new\n"
        )
    return "".join(parts)


def test_pr_too_large_posts_neutral_comment_and_returns_200(
    s3_bucket: Any, deps: _HandlerDeps
) -> None:
    deps.fetch_pr_diff.return_value = _build_multi_file_diff(51)

    event = _signed_event(_sample_payload())
    response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 200
    deps.analyze_diff.assert_not_called()
    deps.publish_review.assert_not_called()
    deps.post_issue_comment.assert_called_once()
    body_arg = deps.post_issue_comment.call_args.args[2]
    assert "51 eligible file" in body_arg
    assert "50-file" in body_arg
    log_call = deps.emit_structured_log.call_args
    assert log_call.kwargs["reason"] == "too_large"


# ---------------------------------------------------------------------------
# Scenario 6 — empty PR (no eligible files) → 200 + neutral comment
# ---------------------------------------------------------------------------


def test_empty_pr_posts_no_changes_comment_and_returns_200(
    s3_bucket: Any, deps: _HandlerDeps
) -> None:
    deps.fetch_pr_diff.return_value = ""  # no diff sections → is_empty

    event = _signed_event(_sample_payload())
    response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 200
    deps.analyze_diff.assert_not_called()
    deps.publish_review.assert_not_called()
    deps.post_issue_comment.assert_called_once()
    assert "No changes to review" in deps.post_issue_comment.call_args.args[2]
    log_call = deps.emit_structured_log.call_args
    assert log_call.kwargs["reason"] == "no_changes"


def test_all_files_filtered_is_treated_as_empty(s3_bucket: Any, deps: _HandlerDeps) -> None:
    """A diff whose sections are all denylisted collapses to is_empty=True."""
    deps.fetch_pr_diff.return_value = (
        "diff --git a/package-lock.json b/package-lock.json\n"
        "--- a/package-lock.json\n"
        "+++ b/package-lock.json\n"
        "@@ -1 +1 @@\n-x\n+y\n"
    )
    event = _signed_event(_sample_payload())
    response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 200
    deps.analyze_diff.assert_not_called()
    assert "No changes to review" in deps.post_issue_comment.call_args.args[2]


# ---------------------------------------------------------------------------
# Scenario 7 — out-of-hunk findings → 200 + failure log, no review
# ---------------------------------------------------------------------------


def test_out_of_hunk_findings_return_200_and_skip_publish(
    s3_bucket: Any, deps: _HandlerDeps
) -> None:
    # line=999 is well outside the hunk range 1..3 that _VALID_DIFF creates.
    deps.analyze_diff.return_value = [_finding(line=999)]

    event = _signed_event(_sample_payload())
    response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 200
    deps.publish_review.assert_not_called()
    deps.emit_metric.assert_any_call("ReviewFailed", 1, {"repo": _REPO, "reason": "out_of_hunk"})
    log_call = deps.emit_structured_log.call_args
    assert log_call.kwargs["reason"] == "out_of_hunk"
    assert log_call.kwargs["out_of_hunk_count"] == 1


def test_partial_out_of_hunk_still_skips_publish(s3_bucket: Any, deps: _HandlerDeps) -> None:
    """Even one out-of-hunk finding aborts the review — no partial publication."""
    deps.analyze_diff.return_value = [
        _finding(line=1),  # valid
        _finding(line=999),  # invalid
    ]

    event = _signed_event(_sample_payload())
    response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 200
    deps.publish_review.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario 8 — dedup hit → 200 (publish returns success + dedup reason)
# ---------------------------------------------------------------------------


def test_dedup_hit_returns_200_no_new_review(s3_bucket: Any, deps: _HandlerDeps) -> None:
    deps.analyze_diff.return_value = [_finding(line=1)]
    deps.publish_review.return_value = review_publisher.PublishResult(
        success=True, review_id=None, skipped_reason="dedup"
    )

    event = _signed_event(_sample_payload())
    response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["review_id"] is None
    assert "dedup" in body["message"]
    log_call = deps.emit_structured_log.call_args
    assert log_call.args[6] == "skipped"
    assert log_call.kwargs["skipped_reason"] == "dedup"


# ---------------------------------------------------------------------------
# Scenario 9 — GitHub rate limit → 200 + neutral issue comment
# ---------------------------------------------------------------------------


def test_rate_limit_on_diff_fetch_posts_neutral_and_returns_200(
    s3_bucket: Any, deps: _HandlerDeps
) -> None:
    from code_review_agent.github_client import RateLimitError

    deps.fetch_pr_diff.side_effect = RateLimitError("exhausted")

    event = _signed_event(_sample_payload())
    response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 200
    deps.post_issue_comment.assert_called_once()
    assert "rate limit" in deps.post_issue_comment.call_args.args[2].lower()
    deps.emit_metric.assert_any_call("ReviewFailed", 1, {"repo": _REPO, "reason": "rate_limit"})


def test_rate_limit_on_review_post_posts_neutral_and_returns_200(
    s3_bucket: Any, deps: _HandlerDeps
) -> None:
    deps.analyze_diff.return_value = [_finding(line=1)]
    deps.publish_review.return_value = review_publisher.PublishResult(
        success=False, review_id=None, skipped_reason="rate_limit"
    )

    event = _signed_event(_sample_payload())
    response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 200
    deps.post_issue_comment.assert_called_once()
    deps.emit_metric.assert_any_call("ReviewFailed", 1, {"repo": _REPO, "reason": "rate_limit"})


# ---------------------------------------------------------------------------
# Scenario 10 — GitHub error (non-rate-limit) → 502
# ---------------------------------------------------------------------------


def test_diff_fetch_error_returns_502(s3_bucket: Any, deps: _HandlerDeps) -> None:
    from code_review_agent.github_client import GitHubFetchError

    deps.fetch_pr_diff.side_effect = GitHubFetchError("server error 500")

    event = _signed_event(_sample_payload())
    response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 502
    deps.emit_metric.assert_any_call(
        "ReviewFailed", 1, {"repo": _REPO, "reason": "diff_fetch_error"}
    )
    deps.analyze_diff.assert_not_called()


def test_review_post_github_error_returns_502(s3_bucket: Any, deps: _HandlerDeps) -> None:
    deps.analyze_diff.return_value = [_finding(line=1)]
    deps.publish_review.return_value = review_publisher.PublishResult(
        success=False, review_id=None, skipped_reason="github_error"
    )

    event = _signed_event(_sample_payload())
    response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 502
    deps.emit_metric.assert_any_call("ReviewFailed", 1, {"repo": _REPO, "reason": "github_error"})


# ---------------------------------------------------------------------------
# Scenario 11 — Bedrock error → 500
# ---------------------------------------------------------------------------


def test_bedrock_client_error_returns_500(s3_bucket: Any, deps: _HandlerDeps) -> None:
    from botocore.exceptions import ClientError

    deps.analyze_diff.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "throttled"}},
        "InvokeModel",
    )

    event = _signed_event(_sample_payload())
    response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 500
    deps.emit_metric.assert_any_call("ReviewFailed", 1, {"repo": _REPO, "reason": "bedrock_error"})
    deps.publish_review.assert_not_called()


def test_bedrock_botocore_error_returns_500(s3_bucket: Any, deps: _HandlerDeps) -> None:
    from botocore.exceptions import EndpointConnectionError

    deps.analyze_diff.side_effect = EndpointConnectionError(
        endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com/"
    )

    event = _signed_event(_sample_payload())
    response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 500


# ---------------------------------------------------------------------------
# Scenario 12 — analysis cache hit → skip Bedrock, still publish
# ---------------------------------------------------------------------------


def test_analysis_cache_hit_skips_fetch_and_bedrock(s3_bucket: Any, deps: _HandlerDeps) -> None:
    """Populate the S3 analysis cache directly, then invoke."""
    from code_review_agent import diff_cache

    # Populate cache for the exact {repo, pr, head_sha} the event will carry.
    diff_cache.put_analysis(_REPO, _PR, _SHA, [_finding(file="x.py", line=5, severity="warning")])
    # Sanity-check that the cache write succeeded before invoking the handler.
    assert diff_cache.get_cached_analysis(_REPO, _PR, _SHA) is not None

    event = _signed_event(_sample_payload())
    response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 200
    deps.fetch_pr_diff.assert_not_called()
    deps.analyze_diff.assert_not_called()
    deps.publish_review.assert_called_once()
    log_call = deps.emit_structured_log.call_args
    assert log_call.kwargs["cached"] is True


def test_analysis_cache_miss_but_different_sha_still_fetches(
    s3_bucket: Any, deps: _HandlerDeps
) -> None:
    """Cache entry for a different SHA should not intercept the current one."""
    from code_review_agent import diff_cache

    diff_cache.put_analysis(_REPO, _PR, _OTHER_SHA, [_finding(line=1)])
    event = _signed_event(_sample_payload(head_sha=_SHA))
    response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 200
    deps.fetch_pr_diff.assert_called_once()  # cache miss for _SHA


# ---------------------------------------------------------------------------
# Response shape uniformity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event_builder", "expected_status"),
    [
        (lambda: _signed_event(_sample_payload()), 200),
        (lambda: _signed_event(_sample_payload(), tamper_signature=True), 401),
        (lambda: _signed_event("not json"), 400),
        (lambda: _signed_event(_sample_payload(), event_type="issues"), 200),
    ],
)
def test_response_shape_is_api_gateway_v2_compatible(
    s3_bucket: Any,
    deps: _HandlerDeps,
    event_builder: Any,
    expected_status: int,
) -> None:
    response = handler.lambda_handler(event_builder(), None)
    assert set(response.keys()) == {"statusCode", "headers", "body"}
    assert response["statusCode"] == expected_status
    assert response["headers"]["Content-Type"] == "application/json"
    assert isinstance(response["body"], str)
    json.loads(response["body"])  # body must be valid JSON string
