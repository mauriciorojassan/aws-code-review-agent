"""Bedrock interaction — prompt construction and response parsing."""

from __future__ import annotations

import json
import logging
from typing import Any

import boto3

from code_review_agent.models import Finding

logger = logging.getLogger(__name__)

BEDROCK_MODEL_ID = "anthropic.claude-3-haiku-20240307"


def get_bedrock_client() -> Any:
    """Create Bedrock Runtime client."""
    return boto3.client("bedrock-runtime")


def analyze_diff(diff: str, model_id: str = BEDROCK_MODEL_ID) -> list[Finding]:
    """Analyze a unified diff using Bedrock Claude Haiku.

    Args:
        diff: Unified diff content.
        model_id: Bedrock model identifier.

    Returns:
        List of Finding objects from the analysis.
    """
    client = get_bedrock_client()

    prompt = _build_review_prompt(diff)

    response = client.invoke_model(
        modelId=model_id,
        contentType="application/json",
        accept="application/json",
        body=json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}],
                "system": _system_prompt(),
            }
        ),
    )

    response_body = json.loads(response["body"].read())
    return _parse_findings(response_body)


def _system_prompt() -> str:
    """Return the system prompt for the code reviewer persona."""
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


def _parse_findings(response_body: dict[str, Any]) -> list[Finding]:
    """Parse Bedrock response into Finding objects."""
    content = response_body.get("content", [])
    if not content:
        return []

    text = content[0].get("text", "")

    try:
        # Try to extract JSON from response
        findings_data = json.loads(text)
        if isinstance(findings_data, list):
            return [Finding(**f) for f in findings_data]
    except (json.JSONDecodeError, TypeError):
        logger.warning("Failed to parse Bedrock response as JSON")

    return []
