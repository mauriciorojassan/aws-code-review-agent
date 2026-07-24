"""Tests for Pydantic models deserialization from GitHub webhook payloads."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from code_review_agent.models import WebhookPayload

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
