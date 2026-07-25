"""Bedrock interaction — prompt construction and response parsing.

Cost governance is enforced at this boundary: only Claude Haiku model
identifiers are accepted, and the module-level Bedrock client is
lazy-initialized so warm Lambda invocations do not repeatedly construct
new clients.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import boto3
from botocore.config import Config
from pydantic import ValidationError

from code_review_agent.models import Finding

logger = logging.getLogger(__name__)

# Default model when ``BEDROCK_MODEL_ID`` is not set. Mirrors the value
# documented in ``design.md`` §1 and the ``template.yaml`` env block.
_DEFAULT_MODEL_ID = "anthropic.claude-3-haiku-20240307"

# Accepted Haiku family variants:
#   - anthropic.claude-3-haiku* (any sub-version)
#   - anthropic.claude-3-5-haiku* (any sub-version)
#   - anthropic.claude-3-7-haiku* (any sub-version)
# Anchored at the start of the id so ARN-style overrides that merely embed
# ``anthropic.claude-3-haiku`` as a substring are rejected.
_HAIKU_RE = re.compile(r"^anthropic\.claude-3(-5|-7)?-haiku")

# Sentinel used to distinguish "caller omitted the argument" (→ resolve from
# env var) from "caller explicitly passed ``None`` / empty" (→ ValueError).
_UNSET: Any = object()

# Timeouts sized to fit inside the 30-second Lambda budget: 3s to establish
# the TLS session + up to 25s for Bedrock to stream a response = 28s worst
# case, leaving 2s for shutdown and structured-log flush. ``max_attempts=1``
# disables botocore's automatic retry — a retry inside a 30s budget would
# always blow past it, and Lambda's own invocation-retry policy is a more
# appropriate lever for transient Bedrock errors.
_BEDROCK_CLIENT_CONFIG = Config(
    connect_timeout=3,
    read_timeout=25,
    retries={"max_attempts": 1, "mode": "standard"},
)

# Module-level lazy singleton. Reset to ``None`` in tests via monkeypatch.
_client: Any | None = None


def _resolve_model_id() -> str:
    """Return the effective model id from the ``BEDROCK_MODEL_ID`` env var.

    Falls back to :data:`_DEFAULT_MODEL_ID` when the variable is unset or
    empty. Resolution happens at call time so tests and Lambda cold starts
    both see the current environment.
    """
    return os.environ.get("BEDROCK_MODEL_ID") or _DEFAULT_MODEL_ID


def _validate_model_id(model_id: Any) -> str:
    """Reject any model id that is not a Claude Haiku variant.

    Raises :class:`ValueError` on ``None``, non-string, empty, or whitespace
    input, and on any model id that does not match :data:`_HAIKU_RE`.
    """
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError(
            "BEDROCK_MODEL_ID must be a non-empty Claude Haiku model id; " f"got {model_id!r}"
        )
    if not _HAIKU_RE.match(model_id):
        raise ValueError(
            "Only Claude Haiku models are permitted for cost governance; " f"got {model_id!r}"
        )
    return model_id


def get_bedrock_client() -> Any:
    """Return the module-level Bedrock Runtime client, creating it lazily.

    The client is cached for the lifetime of the process (or until a test
    resets ``_client`` to ``None``). This mirrors the pattern used by
    :mod:`code_review_agent.diff_cache` and avoids the per-invocation
    ``boto3.client`` construction cost on warm Lambda containers.

    The client is configured with :data:`_BEDROCK_CLIENT_CONFIG` so that
    a hanging Bedrock call surfaces as a catchable ``ReadTimeoutError``
    before Lambda kills the container at the 30-second budget boundary.
    """
    global _client
    if _client is None:
        _client = boto3.client("bedrock-runtime", config=_BEDROCK_CLIENT_CONFIG)
    return _client


def analyze_diff(diff: str, model_id: Any = _UNSET) -> list[Finding]:
    """Analyze a unified diff using Bedrock Claude Haiku.

    Args:
        diff: Unified diff content.
        model_id: Bedrock model identifier. When omitted, resolved from the
            ``BEDROCK_MODEL_ID`` env var (default
            ``anthropic.claude-3-haiku-20240307``). Explicit ``None``, empty
            string, whitespace-only, or non-Haiku values raise
            :class:`ValueError` before any Bedrock call is made.

    Returns:
        List of :class:`Finding` objects extracted from the model response.
        Malformed responses yield an empty list; no exception is propagated
        from response parsing.

    Raises:
        ValueError: If ``model_id`` does not resolve to a Claude Haiku
            variant.
    """
    if model_id is _UNSET:
        model_id = _resolve_model_id()
    resolved = _validate_model_id(model_id)

    client = get_bedrock_client()

    response = client.invoke_model(
        modelId=resolved,
        contentType="application/json",
        accept="application/json",
        body=json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": _build_review_prompt(diff)}],
                "system": _system_prompt(),
            }
        ),
    )

    response_body = json.loads(response["body"].read())
    return _parse_findings(response_body)


def _system_prompt() -> str:
    """Return the system prompt for the code-reviewer persona."""
    return (
        "You are a senior code reviewer. Analyze the provided diff and return "
        "structured findings as a JSON array. Each finding must have: "
        '"file" (string), "line" (int), "severity" ("error"|"warning"|"info"), '
        '"message" (string), "suggestion" (string or null). '
        "Focus on bugs, security issues, and significant style problems. "
        "Be concise and actionable."
    )


def _build_review_prompt(diff: str) -> str:
    """Construct the user prompt with the diff content."""
    return f"Review this pull request diff and provide findings:\n\n```diff\n{diff}\n```"


def _parse_findings(response_body: Any) -> list[Finding]:
    """Parse a Bedrock response body into :class:`Finding` objects.

    Contract: **never raises**. Every failure path logs a warning at the
    ``code_review_agent.reviewer`` logger and returns ``[]`` (or, for
    per-element failures, skips the offending element and preserves the
    valid ones).

    Handled malformations:
      * ``response_body`` is not a JSON object.
      * Missing / empty / non-list ``content``.
      * ``content[0]`` is not an object.
      * Missing / non-string / empty ``content[0].text``.
      * ``text`` is not valid JSON.
      * Parsed JSON is not an array.
      * Individual elements are not dicts or fail :class:`Finding` validation.
    """
    if not isinstance(response_body, dict):
        logger.warning(
            "Bedrock response is not a JSON object; got %s",
            type(response_body).__name__,
        )
        return []

    content = response_body.get("content")
    if not isinstance(content, list) or not content:
        logger.warning("Bedrock response has missing, empty, or non-list 'content'")
        return []

    first = content[0]
    if not isinstance(first, dict):
        logger.warning("Bedrock response content[0] is not an object")
        return []

    text = first.get("text")
    if not isinstance(text, str) or not text:
        logger.warning("Bedrock response content[0].text is missing, empty, or not a string")
        return []

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning("Bedrock response text is not valid JSON: %s", e)
        return []

    if not isinstance(parsed, list):
        logger.warning(
            "Bedrock response text is not a JSON array; got %s",
            type(parsed).__name__,
        )
        return []

    findings: list[Finding] = []
    for index, item in enumerate(parsed):
        if not isinstance(item, dict):
            logger.warning(
                "Skipping non-object finding at index %d: type=%s",
                index,
                type(item).__name__,
            )
            continue
        try:
            findings.append(Finding(**item))
        except ValidationError as e:
            logger.warning("Skipping invalid finding at index %d: %s", index, e)
    return findings
