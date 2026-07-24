"""Pydantic models for webhook payloads and review findings."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class Finding(BaseModel):
    """A single review finding on a specific line."""

    file: str
    line: int
    severity: Literal["error", "warning", "info"]
    message: str
    suggestion: str | None = None


class RepoInfo(BaseModel):
    """Repository information from webhook payload."""

    full_name: str
    owner: str
    name: str


class PRInfo(BaseModel):
    """Pull request information from webhook payload."""

    number: int
    head_sha: str
    title: str
    diff_url: str


class WebhookPayload(BaseModel):
    """Parsed GitHub webhook payload for PR events."""

    action: str
    number: int
    repository: RepoInfo
    pull_request: PRInfo


class ReviewResult(BaseModel):
    """Result of a code review analysis."""

    findings: list[Finding]
    cached: bool = False
