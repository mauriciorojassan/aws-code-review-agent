"""Tests for Pydantic models deserialization from GitHub webhook payloads."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from code_review_agent.models import Finding, WebhookPayload

_FIXTURE = Path(__file__).parent / "fixtures" / "sample_webhook_payload.json"


def _load_sample() -> dict[str, Any]:
    return json.loads(_FIXTURE.read_text())


def test_webhook_payload_deserializes_github_sample() -> None:
    data = _load_sample()
    payload = WebhookPayload(**data)
    assert payload.action == "opened"


def test_repo_info_extracts_owner_login() -> None:
    data = _load_sample()
    payload = WebhookPayload(**data)
    assert payload.repository.owner.login == "octocat"


def test_pr_info_extracts_head_sha() -> None:
    data = _load_sample()
    payload = WebhookPayload(**data)
    assert payload.pull_request.head.sha == "abc123def456789012345678901234567890abcd"


def test_extra_fields_silently_ignored() -> None:
    data = _load_sample()
    data["repository"]["id"] = 123456  # extra field GitHub sometimes sends
    payload = WebhookPayload(**data)
    assert payload.repository.full_name == "octocat/Hello-World"


# ---------------------------------------------------------------------------
# Finding schema — line >= 1 invariant
# ---------------------------------------------------------------------------


def _base_finding_kwargs(line: int) -> dict[str, Any]:
    return {
        "file": "src/main.py",
        "line": line,
        "severity": "warning",
        "message": "check this",
    }


def test_finding_accepts_positive_line() -> None:
    f = Finding(**_base_finding_kwargs(line=1))
    assert f.line == 1


@pytest.mark.parametrize("bad_line", [0, -1, -100])
def test_finding_rejects_non_positive_line(bad_line: int) -> None:
    """``line`` must be >= 1; the constraint lives at the model boundary so
    downstream code (diff validator, publisher) can trust the invariant."""
    with pytest.raises(ValidationError, match="greater than 0"):
        Finding(**_base_finding_kwargs(line=bad_line))
