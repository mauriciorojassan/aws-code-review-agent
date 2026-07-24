"""Tests for the webhook validator module."""

from __future__ import annotations

import hashlib
import hmac

from code_review_agent.webhook_validator import (
    filter_action,
    filter_event,
    validate_signature,
)

_SECRET = b"test-secret"
_PAYLOAD = b'{"action":"opened","number":1}'


def _sign(secret: bytes, payload: bytes) -> str:
    digest = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# --- validate_signature ---------------------------------------------------


def test_valid_signature_matches() -> None:
    header = _sign(_SECRET, _PAYLOAD)
    assert validate_signature(_SECRET, _PAYLOAD, header) is True


def test_wrong_secret_returns_false() -> None:
    header = _sign(b"other-secret", _PAYLOAD)
    assert validate_signature(_SECRET, _PAYLOAD, header) is False


def test_tampered_payload_returns_false() -> None:
    header = _sign(_SECRET, _PAYLOAD)
    tampered = _PAYLOAD + b" "
    assert validate_signature(_SECRET, tampered, header) is False


def test_none_header_returns_false() -> None:
    assert validate_signature(_SECRET, _PAYLOAD, None) is False


def test_empty_header_returns_false() -> None:
    assert validate_signature(_SECRET, _PAYLOAD, "") is False


def test_header_with_wrong_digest_returns_false() -> None:
    assert validate_signature(_SECRET, _PAYLOAD, "sha256=deadbeef") is False


def test_header_with_empty_digest_returns_false() -> None:
    assert validate_signature(_SECRET, _PAYLOAD, "sha256=") is False


def test_v1_prefix_returns_false() -> None:
    digest = hmac.new(_SECRET, _PAYLOAD, hashlib.sha256).hexdigest()
    assert validate_signature(_SECRET, _PAYLOAD, f"v1={digest}") is False


def test_missing_prefix_returns_false() -> None:
    digest = hmac.new(_SECRET, _PAYLOAD, hashlib.sha256).hexdigest()
    assert validate_signature(_SECRET, _PAYLOAD, digest) is False


# --- filter_event ---------------------------------------------------------


def test_filter_event_pull_request() -> None:
    assert filter_event("pull_request") == (True, "pull_request")


def test_filter_event_none() -> None:
    assert filter_event(None) == (False, "missing_event_header")


def test_filter_event_empty_string() -> None:
    assert filter_event("") == (False, "ignored_event_type")


def test_filter_event_push() -> None:
    assert filter_event("push") == (False, "ignored_event_type")


def test_filter_event_issues() -> None:
    assert filter_event("issues") == (False, "ignored_event_type")


def test_filter_event_pull_request_review() -> None:
    assert filter_event("pull_request_review") == (False, "ignored_event_type")


# --- filter_action --------------------------------------------------------


def test_filter_action_opened() -> None:
    assert filter_action("opened") == (True, "opened")


def test_filter_action_synchronize() -> None:
    assert filter_action("synchronize") == (True, "synchronize")


def test_filter_action_none() -> None:
    assert filter_action(None) == (False, "missing_action")


def test_filter_action_empty_string() -> None:
    assert filter_action("") == (False, "missing_action")


def test_filter_action_closed() -> None:
    assert filter_action("closed") == (False, "ignored_action")


def test_filter_action_edited() -> None:
    assert filter_action("edited") == (False, "ignored_action")


def test_filter_action_reopened() -> None:
    assert filter_action("reopened") == (False, "ignored_action")


def test_filter_action_labeled() -> None:
    assert filter_action("labeled") == (False, "ignored_action")


def test_filter_action_ready_for_review() -> None:
    assert filter_action("ready_for_review") == (False, "ignored_action")
