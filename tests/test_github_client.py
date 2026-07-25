"""Tests for :mod:`code_review_agent.github_client`.

HTTP-level mocking via :class:`httpx.MockTransport`; no monkeypatching of
imports and no real network. Requests are captured through a small
``_Recorder`` closure so tests can assert exact URL, method, and header
shape.

The autouse env fixture clears ``GITHUB_TOKEN`` so auth-header assertions
are deterministic; individual tests opt into a token via
:func:`pytest.MonkeyPatch.setenv`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from code_review_agent.github_client import (
    GitHubFetchError,
    RateLimitError,
    fetch_pr_diff,
)

_REPO = "owner/repo"
_PR = 42
_DIFF_URL_PATH = f"/repos/{_REPO}/pulls/{_PR}"
_SAMPLE_DIFF = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_github_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


class _Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []


def _build_client(
    handler: Callable[[httpx.Request], httpx.Response],
    recorder: _Recorder | None = None,
) -> httpx.Client:
    def _wrapped(request: httpx.Request) -> httpx.Response:
        if recorder is not None:
            recorder.requests.append(request)
        return handler(request)

    return httpx.Client(transport=httpx.MockTransport(_wrapped))


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_fetch_pr_diff_returns_diff_text() -> None:
    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_SAMPLE_DIFF)

    client = _build_client(handler, recorder)
    result = fetch_pr_diff(_REPO, _PR, client=client)

    assert result == _SAMPLE_DIFF
    assert len(recorder.requests) == 1


def test_fetch_pr_diff_hits_expected_url_and_headers() -> None:
    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_SAMPLE_DIFF)

    client = _build_client(handler, recorder)
    fetch_pr_diff(_REPO, _PR, client=client)

    req = recorder.requests[0]
    assert req.method == "GET"
    assert str(req.url) == f"https://api.github.com/repos/{_REPO}/pulls/{_PR}"
    assert req.headers.get("Accept") == "application/vnd.github.v3.diff"
    assert req.headers.get("X-GitHub-Api-Version") == "2022-11-28"


# ---------------------------------------------------------------------------
# Auth header wiring
# ---------------------------------------------------------------------------


def test_authorization_header_from_kwarg() -> None:
    recorder = _Recorder()
    client = _build_client(lambda _r: httpx.Response(200, text="x"), recorder)

    fetch_pr_diff(
        _REPO,
        _PR,
        client=client,
        token="test-token",  # noqa: S106 -- test literal
    )

    assert recorder.requests[0].headers.get("Authorization") == "Bearer test-token"


def test_authorization_header_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "env-token")
    recorder = _Recorder()
    client = _build_client(lambda _r: httpx.Response(200, text="x"), recorder)

    fetch_pr_diff(_REPO, _PR, client=client)

    assert recorder.requests[0].headers.get("Authorization") == "Bearer env-token"


def test_no_authorization_header_when_token_unset() -> None:
    recorder = _Recorder()
    client = _build_client(lambda _r: httpx.Response(200, text="x"), recorder)

    fetch_pr_diff(_REPO, _PR, client=client)

    assert "Authorization" not in recorder.requests[0].headers


def test_explicit_empty_string_token_suppresses_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "env-token")
    recorder = _Recorder()
    client = _build_client(lambda _r: httpx.Response(200, text="x"), recorder)

    fetch_pr_diff(_REPO, _PR, client=client, token="")

    assert "Authorization" not in recorder.requests[0].headers


# ---------------------------------------------------------------------------
# Rate-limit detection — no retry, RateLimitError
# ---------------------------------------------------------------------------


def test_403_with_ratelimit_remaining_zero_raises_rate_limit() -> None:
    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={"X-RateLimit-Remaining": "0"},
            json={"message": "API rate limit exceeded"},
        )

    client = _build_client(handler, recorder)
    with pytest.raises(RateLimitError):
        fetch_pr_diff(_REPO, _PR, client=client)
    assert len(recorder.requests) == 1  # no retry


def test_bare_429_raises_rate_limit() -> None:
    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    client = _build_client(handler, recorder)
    with pytest.raises(RateLimitError):
        fetch_pr_diff(_REPO, _PR, client=client)
    assert len(recorder.requests) == 1


def test_403_with_retry_after_raises_rate_limit() -> None:
    """Secondary rate limit surfaces as 403 + ``Retry-After``."""
    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={"Retry-After": "60"})

    client = _build_client(handler, recorder)
    with pytest.raises(RateLimitError):
        fetch_pr_diff(_REPO, _PR, client=client)
    assert len(recorder.requests) == 1


def test_429_with_retry_after_raises_rate_limit() -> None:
    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "30"})

    client = _build_client(handler, recorder)
    with pytest.raises(RateLimitError):
        fetch_pr_diff(_REPO, _PR, client=client)
    assert len(recorder.requests) == 1


def test_rate_limit_error_is_github_fetch_error() -> None:
    """Callers that don't care about the distinction can catch the parent."""
    assert issubclass(RateLimitError, GitHubFetchError)


# ---------------------------------------------------------------------------
# 5xx retry semantics
# ---------------------------------------------------------------------------


def test_500_twice_raises_github_fetch_error() -> None:
    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal")

    client = _build_client(handler, recorder)
    with pytest.raises(GitHubFetchError, match="server error 500"):
        fetch_pr_diff(_REPO, _PR, client=client)
    assert len(recorder.requests) == 2  # initial + one retry


def test_502_then_success_returns_diff() -> None:
    recorder = _Recorder()
    responses = iter(
        [
            httpx.Response(502, text="bad gateway"),
            httpx.Response(200, text=_SAMPLE_DIFF),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    client = _build_client(handler, recorder)
    result = fetch_pr_diff(_REPO, _PR, client=client)

    assert result == _SAMPLE_DIFF
    assert len(recorder.requests) == 2


def test_503_gets_one_retry_then_fails() -> None:
    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = _build_client(handler, recorder)
    with pytest.raises(GitHubFetchError):
        fetch_pr_diff(_REPO, _PR, client=client)
    assert len(recorder.requests) == 2


# ---------------------------------------------------------------------------
# Transport-error retry semantics
# ---------------------------------------------------------------------------


def test_connect_error_twice_raises_github_fetch_error() -> None:
    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network unreachable")

    client = _build_client(handler, recorder)
    with pytest.raises(GitHubFetchError, match="transport failure"):
        fetch_pr_diff(_REPO, _PR, client=client)
    assert len(recorder.requests) == 2


def test_connect_error_then_success_returns_diff() -> None:
    recorder = _Recorder()
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise httpx.ConnectError("transient")
        return httpx.Response(200, text=_SAMPLE_DIFF)

    client = _build_client(handler, recorder)
    result = fetch_pr_diff(_REPO, _PR, client=client)

    assert result == _SAMPLE_DIFF
    assert len(recorder.requests) == 2


# ---------------------------------------------------------------------------
# 4xx (non-rate-limit) — no retry
# ---------------------------------------------------------------------------


def test_404_raises_immediately_without_retry() -> None:
    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    client = _build_client(handler, recorder)
    with pytest.raises(GitHubFetchError, match="client error 404"):
        fetch_pr_diff(_REPO, _PR, client=client)
    assert len(recorder.requests) == 1  # no retry on 4xx


def test_403_without_rate_limit_signal_raises_immediately() -> None:
    """403 with ``X-RateLimit-Remaining>0`` is a permissions error, not rate limit."""
    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={"X-RateLimit-Remaining": "999"})

    client = _build_client(handler, recorder)
    with pytest.raises(GitHubFetchError, match="client error 403"):
        fetch_pr_diff(_REPO, _PR, client=client)
    assert len(recorder.requests) == 1  # deterministic, no retry


def test_401_raises_immediately() -> None:
    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    client = _build_client(handler, recorder)
    with pytest.raises(GitHubFetchError, match="client error 401"):
        fetch_pr_diff(_REPO, _PR, client=client)
    assert len(recorder.requests) == 1


# ---------------------------------------------------------------------------
# Auto-created client cleanup
# ---------------------------------------------------------------------------


def test_fetch_pr_diff_creates_and_closes_client_when_none_provided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    close_calls = {"n": 0}
    real_client_cls = httpx.Client

    def factory(*_args: Any, **_kwargs: Any) -> httpx.Client:
        c = real_client_cls(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, text=_SAMPLE_DIFF))
        )
        original_close = c.close

        def _tracked_close() -> None:
            close_calls["n"] += 1
            original_close()

        c.close = _tracked_close  # type: ignore[method-assign]
        return c

    monkeypatch.setattr("code_review_agent.github_client.httpx.Client", factory)

    result = fetch_pr_diff(_REPO, _PR)

    assert result == _SAMPLE_DIFF
    assert close_calls["n"] == 1
