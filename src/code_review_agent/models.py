"""Pydantic models for webhook payloads and review findings."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Finding(BaseModel):
    """A single review finding on a specific line."""

    file: str
    # ``line`` is the absolute 1-indexed line number in the post-state (new)
    # file, matching the GitHub review API. Enforced at the model layer so
    # that downstream code (diff validator, review publisher) can trust the
    # invariant without redundant checks.
    line: int = Field(gt=0)
    severity: Literal["error", "warning", "info"]
    message: str
    suggestion: str | None = None


class OwnerInfo(BaseModel):
    """GitHub repository owner metadata."""

    login: str


class HeadInfo(BaseModel):
    """PR head branch metadata."""

    sha: str
    ref: str | None = None


class RepoInfo(BaseModel):
    """Repository information from a GitHub webhook payload."""

    full_name: str
    name: str
    owner: OwnerInfo


class PRInfo(BaseModel):
    """Pull request information from a GitHub webhook payload."""

    number: int
    title: str
    head: HeadInfo
    diff_url: str | None = None


class WebhookPayload(BaseModel):
    """Parsed GitHub webhook payload for pull_request events."""

    model_config = ConfigDict(extra="ignore")

    action: str
    repository: RepoInfo
    pull_request: PRInfo


class ReviewResult(BaseModel):
    """Result of a code review analysis."""

    findings: list[Finding]
    cached: bool = False
