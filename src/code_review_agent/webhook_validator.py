"""HMAC-SHA256 webhook signature validation and event/action filtering."""

from __future__ import annotations

import hashlib
import hmac

_SIGNATURE_PREFIX = "sha256="
_ALLOWED_ACTIONS = frozenset({"opened", "synchronize"})


def validate_signature(secret: bytes, payload: bytes, signature_header: str | None) -> bool:
    """Return True iff the header carries a valid GitHub HMAC-SHA256 signature.

    Expected header format: ``sha256=<hex-digest>``. Returns False for any
    malformed input; never raises.
    """
    if signature_header is None or not signature_header.startswith(_SIGNATURE_PREFIX):
        return False

    provided_hex = signature_header[len(_SIGNATURE_PREFIX) :]
    expected_hex = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected_hex, provided_hex)


def filter_event(event_header: str | None) -> tuple[bool, str]:
    """Return (accepted, reason) for the ``X-GitHub-Event`` header value."""
    if event_header is None:
        return (False, "missing_event_header")
    if event_header == "pull_request":
        return (True, "pull_request")
    return (False, "ignored_event_type")


def filter_action(action: str | None) -> tuple[bool, str]:
    """Return (accepted, reason) for a ``pull_request`` webhook action."""
    if not action:
        return (False, "missing_action")
    if action in _ALLOWED_ACTIONS:
        return (True, action)
    return (False, "ignored_action")
