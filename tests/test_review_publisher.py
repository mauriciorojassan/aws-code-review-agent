"""Tests for :mod:`code_review_agent.review_publisher`.

Mocking strategy: every test builds a :class:`httpx.MockTransport` with a
handler that decides responses per request and records what it saw. The
transport is wrapped in an :class:`httpx.Client` and injected via the
``client=`` keyword arg of :func:`publish_review`, so no monkeypatch of
imports, no global state, and no real HTTP is ever touched.

The autouse env fixture clears ``GITHUB_TOKEN`` so token-related assertions
are deterministic; individual tests opt into a token via ``monkeypatch``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from code_review_agent.models import Finding
from code_review_agent.review_publisher import (
    PublishResult,
    _build_review_body,
    _dedup_marker,
    _finding_to_comment,
    _sort_findings,
    publish_review,
)

_REPO = "owner/repo"
_PR = 42
_SHA = "abc123def456789012345678901234567890abcd"
_OTHER_SHA = "1111111111111111111111111111111111111111"


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_github_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure GITHUB_TOKEN is not leaked from the host env into tests."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


@pytest.fixture
def summary_data() -> dict[str, Any]:
    """Minimal ``summary_data`` payload for tests that don't care about it."""
    return {"excluded_files": 0, "truncated": False}


def _finding(
    file: str = "src/main.py",
    line: int = 10,
    severity: str = "warning",
    message: str = "check this",
    suggestion: str | None = None,
) -> Finding:
    return Finding(
        file=file,
        line=line,
        severity=severity,  # type: ignore[arg-type]
        message=message,
        suggestion=suggestion,
    )


class _Recorder:
    """Accumulates every request the mock transport observes."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    @property
    def get_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "GET"]

    @property
    def post_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST"]

    def posted_payload(self, index: int = 0) -> dict[str, Any]:
        """Return the JSON body of the ``index``-th POST request."""
        return json.loads(self.post_requests[index].content.decode("utf-8"))


def _build_client(
    handler: Callable[[httpx.Request], httpx.Response],
    recorder: _Recorder | None = None,
) -> httpx.Client:
    """Wrap ``handler`` in a MockTransport-backed :class:`httpx.Client`."""

    def _wrapped(request: httpx.Request) -> httpx.Response:
        if recorder is not None:
            recorder.requests.append(request)
        return handler(request)

    return httpx.Client(transport=httpx.MockTransport(_wrapped))


def _standard_router(
    *,
    existing_reviews: list[dict[str, Any]] | None = None,
    post_response: httpx.Response | Callable[[httpx.Request], httpx.Response] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Build a handler that answers the dedup GET and the review POST.

    ``post_response`` may be a static :class:`httpx.Response` or a callable
    that is invoked per POST request (useful for retry scenarios that need
    to return different responses across attempts).
    """
    reviews = existing_reviews if existing_reviews is not None else []
    post_calls = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith(
            f"/repos/{_REPO}/pulls/{_PR}/reviews"
        ):
            return httpx.Response(200, json=reviews)
        if request.method == "POST" and request.url.path.endswith(
            f"/repos/{_REPO}/pulls/{_PR}/reviews"
        ):
            post_calls["n"] += 1
            if callable(post_response):
                return post_response(request)
            if post_response is not None:
                return post_response
            return httpx.Response(201, json={"id": 999})
        return httpx.Response(404, json={"message": "route not mocked"})

    return _handler


# ---------------------------------------------------------------------------
# Scenario 1 — dedup hit
# ---------------------------------------------------------------------------


def test_dedup_hit_returns_dedup_and_skips_post(summary_data: dict[str, Any]) -> None:
    """Existing review body containing the head-SHA marker skips the POST."""
    recorder = _Recorder()
    existing = [
        {"id": 1, "body": f"prior review\n\n{_dedup_marker(_SHA)}"},
    ]
    client = _build_client(_standard_router(existing_reviews=existing), recorder)

    result = publish_review(_REPO, _PR, _SHA, [_finding()], summary_data, client=client)

    assert result == PublishResult(success=True, review_id=None, skipped_reason="dedup")
    assert len(recorder.get_requests) == 1
    assert len(recorder.post_requests) == 0


def test_dedup_marker_for_different_sha_does_not_block(
    summary_data: dict[str, Any],
) -> None:
    """A dedup marker for a *different* SHA must not suppress the current post."""
    recorder = _Recorder()
    existing = [{"id": 7, "body": f"stale\n{_dedup_marker(_OTHER_SHA)}"}]
    client = _build_client(_standard_router(existing_reviews=existing), recorder)

    result = publish_review(_REPO, _PR, _SHA, [_finding()], summary_data, client=client)

    assert result.success is True
    assert result.review_id == "999"
    assert len(recorder.post_requests) == 1


def test_dedup_ignores_reviews_without_marker(summary_data: dict[str, Any]) -> None:
    """Non-bot reviews (no marker) never block posting."""
    recorder = _Recorder()
    existing = [
        {"id": 3, "body": "LGTM"},
        {"id": 4, "body": None},
        {"id": 5, "body": "another human review"},
    ]
    client = _build_client(_standard_router(existing_reviews=existing), recorder)

    result = publish_review(_REPO, _PR, _SHA, [_finding()], summary_data, client=client)

    assert result.success is True
    assert len(recorder.post_requests) == 1


def test_dedup_check_500_proceeds_to_post(summary_data: dict[str, Any]) -> None:
    """A dedup-check failure is treated as 'unknown → post'."""
    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(500, text="oops")
        return httpx.Response(201, json={"id": 42})

    client = _build_client(handler, recorder)
    result = publish_review(_REPO, _PR, _SHA, [], summary_data, client=client)

    assert result.success is True
    assert result.review_id == "42"
    assert len(recorder.post_requests) == 1


def test_dedup_check_transport_error_proceeds_to_post(
    summary_data: dict[str, Any],
) -> None:
    """A transport error on the dedup GET is not fatal to the pipeline."""
    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            raise httpx.ConnectError("boom")
        return httpx.Response(201, json={"id": 42})

    client = _build_client(handler, recorder)
    result = publish_review(_REPO, _PR, _SHA, [], summary_data, client=client)

    assert result.success is True
    assert len(recorder.post_requests) == 1


def test_dedup_check_non_list_response_proceeds(summary_data: dict[str, Any]) -> None:
    """When the reviews endpoint returns a JSON object instead of a list, post anyway."""
    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"unexpected": "shape"})
        return httpx.Response(201, json={"id": 88})

    client = _build_client(handler, recorder)
    result = publish_review(_REPO, _PR, _SHA, [], summary_data, client=client)

    assert result.success is True
    assert result.review_id == "88"


def test_dedup_check_non_json_response_proceeds(summary_data: dict[str, Any]) -> None:
    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="not json at all !!!")
        return httpx.Response(201, json={"id": 12})

    client = _build_client(handler, recorder)
    result = publish_review(_REPO, _PR, _SHA, [], summary_data, client=client)

    assert result.success is True


# ---------------------------------------------------------------------------
# Scenario 2 — successful post, <20 findings (no overflow)
# ---------------------------------------------------------------------------


def test_successful_post_returns_review_id(summary_data: dict[str, Any]) -> None:
    recorder = _Recorder()
    client = _build_client(_standard_router(), recorder)

    result = publish_review(
        _REPO, _PR, _SHA, [_finding(severity="info")], summary_data, client=client
    )

    assert result == PublishResult(success=True, review_id="999", skipped_reason=None)


def test_few_findings_all_inline_no_overflow_block(
    summary_data: dict[str, Any],
) -> None:
    """3 findings → 3 inline comments, no overflow block in body."""
    recorder = _Recorder()
    client = _build_client(_standard_router(), recorder)

    findings = [
        _finding(file="a.py", line=1, severity="error", message="e1"),
        _finding(file="b.py", line=2, severity="warning", message="w1"),
        _finding(file="c.py", line=3, severity="info", message="i1"),
    ]
    publish_review(_REPO, _PR, _SHA, findings, summary_data, client=client)

    payload = recorder.posted_payload()
    assert len(payload["comments"]) == 3
    assert "Additional findings" not in payload["body"]


def test_inline_comment_shape() -> None:
    """Inline comments carry ``path``, ``line``, ``side: RIGHT``, ``body`` fields."""
    f = _finding(
        file="src/x.py",
        line=17,
        severity="error",
        message="null deref",
        suggestion="guard against None",
    )
    comment = _finding_to_comment(f)
    assert comment == {
        "path": "src/x.py",
        "line": 17,
        "side": "RIGHT",
        "body": "**[ERROR]** null deref\n\n💡 guard against None",
    }


def test_inline_comment_without_suggestion_has_no_suggestion_block() -> None:
    comment = _finding_to_comment(_finding(suggestion=None))
    assert "💡" not in comment["body"]


# ---------------------------------------------------------------------------
# Scenario 3 — >20 findings (overflow to body)
# ---------------------------------------------------------------------------


def test_twenty_five_findings_split_20_inline_5_overflow(
    summary_data: dict[str, Any],
) -> None:
    recorder = _Recorder()
    client = _build_client(_standard_router(), recorder)

    findings = [
        _finding(file=f"f{i:02d}.py", line=i + 1, severity="warning", message=f"m{i}")
        for i in range(25)
    ]
    publish_review(_REPO, _PR, _SHA, findings, summary_data, client=client)

    payload = recorder.posted_payload()
    assert len(payload["comments"]) == 20
    assert "Additional findings (5)" in payload["body"]
    # All 5 overflow findings appear in the body's fenced block.
    for i in range(20, 25):
        assert f"f{i:02d}.py:{i + 1}" in payload["body"]


def test_exactly_twenty_findings_no_overflow_block(
    summary_data: dict[str, Any],
) -> None:
    recorder = _Recorder()
    client = _build_client(_standard_router(), recorder)

    findings = [_finding(file=f"f{i}.py", line=1, message=f"m{i}") for i in range(20)]
    publish_review(_REPO, _PR, _SHA, findings, summary_data, client=client)

    payload = recorder.posted_payload()
    assert len(payload["comments"]) == 20
    assert "Additional findings" not in payload["body"]


def test_overflow_prioritizes_by_severity(summary_data: dict[str, Any]) -> None:
    """Errors reach the inline slot before warnings; warnings before info."""
    recorder = _Recorder()
    client = _build_client(_standard_router(), recorder)

    findings = (
        [_finding(file=f"info{i}.py", line=1, severity="info") for i in range(15)]
        + [_finding(file=f"warn{i}.py", line=1, severity="warning") for i in range(10)]
        + [_finding(file=f"err{i}.py", line=1, severity="error") for i in range(5)]
    )
    publish_review(_REPO, _PR, _SHA, findings, summary_data, client=client)

    payload = recorder.posted_payload()
    inline_paths = [c["path"] for c in payload["comments"]]
    # First 20 = all 5 errors + all 10 warnings + first 5 info.
    assert all(p.startswith("err") for p in inline_paths[:5])
    assert all(p.startswith("warn") for p in inline_paths[5:15])
    assert all(p.startswith("info") for p in inline_paths[15:20])


# ---------------------------------------------------------------------------
# Scenario 4 — rate limit
# ---------------------------------------------------------------------------


def test_rate_limit_403_returns_rate_limit_and_does_not_retry(
    summary_data: dict[str, Any],
) -> None:
    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(
            403,
            headers={"X-RateLimit-Remaining": "0"},
            json={"message": "API rate limit exceeded"},
        )

    client = _build_client(handler, recorder)
    result = publish_review(_REPO, _PR, _SHA, [_finding()], summary_data, client=client)

    assert result == PublishResult(success=False, review_id=None, skipped_reason="rate_limit")
    assert len(recorder.post_requests) == 1  # no retry


def test_403_without_rate_limit_header_retries_as_generic_error(
    summary_data: dict[str, Any],
) -> None:
    """403 that is *not* rate-limit exhaustion is treated as a generic error."""
    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        # Header present but with remaining>0 → not the rate-limit signal.
        return httpx.Response(403, headers={"X-RateLimit-Remaining": "42"})

    client = _build_client(handler, recorder)
    result = publish_review(_REPO, _PR, _SHA, [_finding()], summary_data, client=client)

    assert result.skipped_reason == "github_error"
    assert len(recorder.post_requests) == 2  # retried once


def test_429_returns_rate_limit_and_does_not_retry(
    summary_data: dict[str, Any],
) -> None:
    """HTTP 429 is unambiguously a rate-limit status per RFC 6585."""
    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(429, headers={"Retry-After": "60"})

    client = _build_client(handler, recorder)
    result = publish_review(_REPO, _PR, _SHA, [_finding()], summary_data, client=client)

    assert result == PublishResult(success=False, review_id=None, skipped_reason="rate_limit")
    assert len(recorder.post_requests) == 1  # no retry


def test_429_without_headers_still_treated_as_rate_limit(
    summary_data: dict[str, Any],
) -> None:
    """A 429 without a ``Retry-After`` header still short-circuits."""
    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(429)

    client = _build_client(handler, recorder)
    result = publish_review(_REPO, _PR, _SHA, [_finding()], summary_data, client=client)

    assert result.skipped_reason == "rate_limit"
    assert len(recorder.post_requests) == 1


def test_403_with_retry_after_treated_as_secondary_rate_limit(
    summary_data: dict[str, Any],
) -> None:
    """GitHub's secondary (abuse) rate limit surfaces as 403 + ``Retry-After``."""
    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(
            403,
            headers={"Retry-After": "30"},
            json={"message": "You have exceeded a secondary rate limit."},
        )

    client = _build_client(handler, recorder)
    result = publish_review(_REPO, _PR, _SHA, [_finding()], summary_data, client=client)

    assert result.skipped_reason == "rate_limit"
    assert len(recorder.post_requests) == 1  # no retry


# ---------------------------------------------------------------------------
# Scenario 5 — GitHub error + retry semantics
# ---------------------------------------------------------------------------


def test_500_twice_returns_github_error(summary_data: dict[str, Any]) -> None:
    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(500, text="internal")

    client = _build_client(handler, recorder)
    result = publish_review(_REPO, _PR, _SHA, [_finding()], summary_data, client=client)

    assert result == PublishResult(success=False, review_id=None, skipped_reason="github_error")
    assert len(recorder.post_requests) == 2  # initial + one retry


def test_500_then_success_returns_review_id(summary_data: dict[str, Any]) -> None:
    """First POST fails 5xx, retry succeeds — result carries the new review id."""
    recorder = _Recorder()
    responses = iter(
        [
            httpx.Response(502, text="bad gateway"),
            httpx.Response(201, json={"id": 314159}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return next(responses)

    client = _build_client(handler, recorder)
    result = publish_review(_REPO, _PR, _SHA, [_finding()], summary_data, client=client)

    assert result.success is True
    assert result.review_id == "314159"
    assert len(recorder.post_requests) == 2


def test_transport_error_retries_then_succeeds(summary_data: dict[str, Any]) -> None:
    recorder = _Recorder()
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise httpx.ConnectError("transient network")
        return httpx.Response(201, json={"id": 271828})

    client = _build_client(handler, recorder)
    result = publish_review(_REPO, _PR, _SHA, [_finding()], summary_data, client=client)

    assert result.success is True
    assert result.review_id == "271828"


def test_transport_error_twice_returns_github_error(
    summary_data: dict[str, Any],
) -> None:
    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        raise httpx.ConnectError("persistent")

    client = _build_client(handler, recorder)
    result = publish_review(_REPO, _PR, _SHA, [_finding()], summary_data, client=client)

    assert result.skipped_reason == "github_error"


def test_success_with_non_json_body_still_returns_success(
    summary_data: dict[str, Any],
) -> None:
    """A 2xx with garbage body yields success with ``review_id=None``."""
    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(201, text="not json")

    client = _build_client(handler, recorder)
    result = publish_review(_REPO, _PR, _SHA, [_finding()], summary_data, client=client)

    assert result.success is True
    assert result.review_id is None


def test_success_with_non_dict_json_yields_none_id(summary_data: dict[str, Any]) -> None:
    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(201, json=[1, 2, 3])

    client = _build_client(handler, recorder)
    result = publish_review(_REPO, _PR, _SHA, [_finding()], summary_data, client=client)

    assert result.success is True
    assert result.review_id is None


def test_success_with_dict_missing_id_yields_none_id(
    summary_data: dict[str, Any],
) -> None:
    """201 response whose JSON dict lacks the ``id`` key still succeeds."""
    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(201, json={"unexpected": "shape"})

    client = _build_client(handler, recorder)
    result = publish_review(_REPO, _PR, _SHA, [_finding()], summary_data, client=client)

    assert result.success is True
    assert result.review_id is None


def test_dedup_check_skips_non_dict_review_entries(
    summary_data: dict[str, Any],
) -> None:
    """Non-dict elements in the reviews array are ignored, not fatal."""
    recorder = _Recorder()
    existing: list[Any] = [
        "not a dict",
        None,
        123,
        {"id": 1, "body": "human review, no marker"},
    ]
    client = _build_client(_standard_router(existing_reviews=existing), recorder)

    result = publish_review(_REPO, _PR, _SHA, [_finding()], summary_data, client=client)

    assert result.success is True
    assert result.review_id == "999"


# ---------------------------------------------------------------------------
# Zero-findings edge case (US-4)
# ---------------------------------------------------------------------------


def test_zero_findings_still_posts_summary_review(
    summary_data: dict[str, Any],
) -> None:
    recorder = _Recorder()
    client = _build_client(_standard_router(), recorder)

    result = publish_review(_REPO, _PR, _SHA, [], summary_data, client=client)

    assert result.success is True
    payload = recorder.posted_payload()
    assert payload["comments"] == []
    assert "No actionable findings" in payload["body"]


# ---------------------------------------------------------------------------
# Review body assembly
# ---------------------------------------------------------------------------


def test_body_contains_dedup_marker() -> None:
    body = _build_review_body([], [], {"excluded_files": 0}, _SHA)
    assert _dedup_marker(_SHA) in body


def test_body_contains_severity_counts() -> None:
    findings = [
        _finding(severity="error"),
        _finding(severity="error"),
        _finding(severity="warning"),
        _finding(severity="info"),
    ]
    body = _build_review_body(findings, [], {"excluded_files": 0}, _SHA)
    assert "| 🔴 Error | 2 |" in body
    assert "| 🟡 Warning | 1 |" in body
    assert "| 🔵 Info | 1 |" in body


def test_body_contains_excluded_file_count() -> None:
    body = _build_review_body([], [], {"excluded_files": 7}, _SHA)
    assert "Excluded **7** file(s)" in body


def test_body_omits_excluded_block_when_zero() -> None:
    body = _build_review_body([], [], {"excluded_files": 0}, _SHA)
    assert "Excluded" not in body


def test_body_contains_default_truncation_note() -> None:
    body = _build_review_body([], [], {"excluded_files": 0, "truncated": True}, _SHA)
    assert "truncated" in body.lower()


def test_body_uses_custom_truncation_note() -> None:
    body = _build_review_body(
        [],
        [],
        {"excluded_files": 0, "truncated": True, "truncation_note": "Custom note here."},
        _SHA,
    )
    assert "Custom note here." in body


def test_body_overflow_block_rendered_for_extras() -> None:
    findings = [_finding(file="a.py", line=1, message="msg1")]
    overflow = [
        _finding(file="b.py", line=2, severity="error", message="msg2", suggestion="fix"),
        _finding(file="c.py", line=3, severity="info", message="msg3"),
    ]
    body = _build_review_body(findings + overflow, overflow, {}, _SHA)
    assert "Additional findings (2)" in body
    assert "[ERROR] b.py:2 — msg2 — fix" in body
    assert "[INFO] c.py:3 — msg3" in body


def test_body_coerces_bad_excluded_files_value() -> None:
    """Non-int / negative ``excluded_files`` degrades to 0, not a crash."""
    body_nan = _build_review_body([], [], {"excluded_files": "many"}, _SHA)
    body_neg = _build_review_body([], [], {"excluded_files": -5}, _SHA)
    body_none = _build_review_body([], [], {"excluded_files": None}, _SHA)
    for body in (body_nan, body_neg, body_none):
        assert "Excluded" not in body


# ---------------------------------------------------------------------------
# Sort determinism
# ---------------------------------------------------------------------------


def test_sort_findings_by_severity_then_file_then_line() -> None:
    findings = [
        _finding(file="z.py", line=1, severity="info"),
        _finding(file="a.py", line=5, severity="warning"),
        _finding(file="a.py", line=3, severity="warning"),
        _finding(file="b.py", line=1, severity="error"),
        _finding(file="a.py", line=1, severity="error"),
    ]
    result = _sort_findings(findings)
    assert [(f.file, f.line, f.severity) for f in result] == [
        ("a.py", 1, "error"),
        ("b.py", 1, "error"),
        ("a.py", 3, "warning"),
        ("a.py", 5, "warning"),
        ("z.py", 1, "info"),
    ]


# ---------------------------------------------------------------------------
# Auth header wiring
# ---------------------------------------------------------------------------


def test_authorization_header_present_when_token_kwarg(
    summary_data: dict[str, Any],
) -> None:
    recorder = _Recorder()
    client = _build_client(_standard_router(), recorder)

    publish_review(
        _REPO,
        _PR,
        _SHA,
        [_finding()],
        summary_data,
        client=client,
        token="test-token",  # noqa: S106 -- test literal, not a real secret
    )

    for req in recorder.requests:
        assert req.headers.get("Authorization") == "Bearer test-token"


def test_authorization_header_from_env_var(
    monkeypatch: pytest.MonkeyPatch, summary_data: dict[str, Any]
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "env-token")
    recorder = _Recorder()
    client = _build_client(_standard_router(), recorder)

    publish_review(_REPO, _PR, _SHA, [_finding()], summary_data, client=client)

    for req in recorder.requests:
        assert req.headers.get("Authorization") == "Bearer env-token"


def test_no_authorization_header_when_token_unset(
    summary_data: dict[str, Any],
) -> None:
    recorder = _Recorder()
    client = _build_client(_standard_router(), recorder)

    publish_review(_REPO, _PR, _SHA, [_finding()], summary_data, client=client)

    for req in recorder.requests:
        assert "Authorization" not in req.headers


def test_explicit_empty_string_token_suppresses_env(
    monkeypatch: pytest.MonkeyPatch, summary_data: dict[str, Any]
) -> None:
    """Passing ``token=""`` overrides the env var and sends no auth header."""
    monkeypatch.setenv("GITHUB_TOKEN", "env-token")
    recorder = _Recorder()
    client = _build_client(_standard_router(), recorder)

    publish_review(_REPO, _PR, _SHA, [_finding()], summary_data, client=client, token="")

    for req in recorder.requests:
        assert "Authorization" not in req.headers


# ---------------------------------------------------------------------------
# Auto-created client cleanup
# ---------------------------------------------------------------------------


def test_publish_review_creates_and_closes_client_when_none_provided(
    monkeypatch: pytest.MonkeyPatch, summary_data: dict[str, Any]
) -> None:
    """When ``client`` is omitted, a client is instantiated and closed cleanly.

    We stub the ``httpx.Client`` constructor used by the module to hand back
    a MockTransport-backed client and observe that its ``close`` method was
    invoked exactly once.
    """
    close_calls = {"n": 0}
    # Capture the real class BEFORE monkeypatching so the factory can bypass
    # its own patch (otherwise instantiating the mock transport recurses).
    real_client_cls = httpx.Client

    def factory(*_args: Any, **_kwargs: Any) -> httpx.Client:
        c = real_client_cls(transport=httpx.MockTransport(_standard_router()))
        original_close = c.close

        def _tracked_close() -> None:
            close_calls["n"] += 1
            original_close()

        c.close = _tracked_close  # type: ignore[method-assign]
        return c

    monkeypatch.setattr("code_review_agent.review_publisher.httpx.Client", factory)

    result = publish_review(_REPO, _PR, _SHA, [_finding()], summary_data)

    assert result.success is True
    assert close_calls["n"] == 1


# ---------------------------------------------------------------------------
# URL routing sanity
# ---------------------------------------------------------------------------


def test_post_target_url_is_reviews_endpoint(summary_data: dict[str, Any]) -> None:
    recorder = _Recorder()
    client = _build_client(_standard_router(), recorder)

    publish_review(_REPO, _PR, _SHA, [_finding()], summary_data, client=client)

    post = recorder.post_requests[0]
    assert str(post.url) == f"https://api.github.com/repos/{_REPO}/pulls/{_PR}/reviews"


def test_post_body_carries_commit_id_and_event_comment(
    summary_data: dict[str, Any],
) -> None:
    recorder = _Recorder()
    client = _build_client(_standard_router(), recorder)

    publish_review(_REPO, _PR, _SHA, [_finding()], summary_data, client=client)

    payload = recorder.posted_payload()
    assert payload["commit_id"] == _SHA
    assert payload["event"] == "COMMENT"
    assert _dedup_marker(_SHA) in payload["body"]


# ---------------------------------------------------------------------------
# post_issue_comment — best-effort neutral comment
# ---------------------------------------------------------------------------


def _issue_comment_router(
    *, status: int = 201, exc: Exception | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    """Handler that only answers on the issue-comments endpoint."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if exc is not None:
            raise exc
        expected = f"/repos/{_REPO}/issues/{_PR}/comments"
        if request.method == "POST" and request.url.path.endswith(expected):
            return httpx.Response(status, json={"id": 12345})
        return httpx.Response(404, json={"message": "route not mocked"})

    return _handler


def test_post_issue_comment_returns_true_on_success() -> None:
    from code_review_agent.review_publisher import post_issue_comment

    recorder = _Recorder()
    client = _build_client(_issue_comment_router(), recorder)

    result = post_issue_comment(_REPO, _PR, "PR too large to review", client=client)

    assert result is True
    assert len(recorder.post_requests) == 1


def test_post_issue_comment_hits_correct_endpoint_and_body() -> None:
    from code_review_agent.review_publisher import post_issue_comment

    recorder = _Recorder()
    client = _build_client(_issue_comment_router(), recorder)

    post_issue_comment(_REPO, _PR, "No changes to review", client=client)

    req = recorder.post_requests[0]
    assert str(req.url) == f"https://api.github.com/repos/{_REPO}/issues/{_PR}/comments"
    payload = json.loads(req.content.decode("utf-8"))
    assert payload == {"body": "No changes to review"}


def test_post_issue_comment_returns_false_on_4xx() -> None:
    from code_review_agent.review_publisher import post_issue_comment

    recorder = _Recorder()
    client = _build_client(_issue_comment_router(status=404), recorder)

    result = post_issue_comment(_REPO, _PR, "body", client=client)

    assert result is False
    assert len(recorder.post_requests) == 1  # no retry


def test_post_issue_comment_returns_false_on_5xx() -> None:
    from code_review_agent.review_publisher import post_issue_comment

    recorder = _Recorder()
    client = _build_client(_issue_comment_router(status=500), recorder)

    result = post_issue_comment(_REPO, _PR, "body", client=client)

    assert result is False
    assert len(recorder.post_requests) == 1  # no retry — fallback path


def test_post_issue_comment_returns_false_on_transport_error() -> None:
    from code_review_agent.review_publisher import post_issue_comment

    recorder = _Recorder()
    client = _build_client(_issue_comment_router(exc=httpx.ConnectError("unreachable")), recorder)

    result = post_issue_comment(_REPO, _PR, "body", client=client)

    assert result is False


def test_post_issue_comment_never_raises_on_arbitrary_http_error() -> None:
    """Regression guard: even the weirder httpx errors must not propagate."""
    from code_review_agent.review_publisher import post_issue_comment

    client = _build_client(_issue_comment_router(exc=httpx.ReadTimeout("timeout")))

    # Must not raise.
    result = post_issue_comment(_REPO, _PR, "body", client=client)
    assert result is False


def test_post_issue_comment_authorization_from_kwarg() -> None:
    from code_review_agent.review_publisher import post_issue_comment

    recorder = _Recorder()
    client = _build_client(_issue_comment_router(), recorder)

    post_issue_comment(
        _REPO,
        _PR,
        "body",
        client=client,
        token="test-token",  # noqa: S106 -- test literal
    )

    assert recorder.post_requests[0].headers.get("Authorization") == "Bearer test-token"


def test_post_issue_comment_authorization_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from code_review_agent.review_publisher import post_issue_comment

    monkeypatch.setenv("GITHUB_TOKEN", "env-token")
    recorder = _Recorder()
    client = _build_client(_issue_comment_router(), recorder)

    post_issue_comment(_REPO, _PR, "body", client=client)

    assert recorder.post_requests[0].headers.get("Authorization") == "Bearer env-token"


def test_post_issue_comment_no_auth_when_token_unset() -> None:
    from code_review_agent.review_publisher import post_issue_comment

    recorder = _Recorder()
    client = _build_client(_issue_comment_router(), recorder)

    post_issue_comment(_REPO, _PR, "body", client=client)

    assert "Authorization" not in recorder.post_requests[0].headers


def test_post_issue_comment_creates_and_closes_client_when_none_provided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from code_review_agent import review_publisher

    close_calls = {"n": 0}
    real_client_cls = httpx.Client

    def factory(*_args: Any, **_kwargs: Any) -> httpx.Client:
        c = real_client_cls(transport=httpx.MockTransport(_issue_comment_router()))
        original_close = c.close

        def _tracked_close() -> None:
            close_calls["n"] += 1
            original_close()

        c.close = _tracked_close  # type: ignore[method-assign]
        return c

    monkeypatch.setattr("code_review_agent.review_publisher.httpx.Client", factory)

    result = review_publisher.post_issue_comment(_REPO, _PR, "body")

    assert result is True
    assert close_calls["n"] == 1
