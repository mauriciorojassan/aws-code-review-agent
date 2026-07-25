"""AWS Secrets Manager integration — fetch webhook secret + GitHub token.

Design notes
------------

**Env-first resolution.** Every accessor checks its dedicated env var
before consulting Secrets Manager. This keeps local development and the
existing pytest suite frictionless: monkeypatch the env var, no
Secrets Manager mock required. In deployed Lambda the env vars are
unset and ``SECRETS_ARN`` points at the Secrets Manager entry.

**Lazy singletons.** The boto3 client is created once per container on
first use; the parsed secret payload is cached similarly. On a warm
Lambda invocation neither is re-fetched. Both cache slots are
resettable to ``None`` in tests via ``monkeypatch``.

**Fail-quiet on Secrets Manager errors.** Fetch failures
(:class:`ClientError`, :class:`BotoCoreError`, non-JSON payload,
non-dict JSON) are logged at error level and treated as "no secret
present". The handler then makes its own decision — for the webhook
secret that means signature validation returns 401 and the request is
rejected, which is the correct fail-closed behavior.

**Expected secret payload shape:**

.. code-block:: json

    {
        "webhook_secret": "<random-shared-secret-with-github>",
        "github_token":   "<github-app-installation-token-or-PAT>"
    }

Any additional keys are ignored. Missing keys are treated as absent
values, not errors — a partial payload (only ``webhook_secret``, for
example) is a legal deployment state during rotations.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

_WEBHOOK_KEY = "webhook_secret"
_TOKEN_KEY = "github_token"  # noqa: S105 -- payload key name, not a credential

# Module-level lazy singletons; reset to ``None`` in tests via monkeypatch.
_client: Any | None = None
_cache: dict[str, str] | None = None


def get_secretsmanager_client() -> Any:
    """Return the module-level Secrets Manager client, creating it lazily."""
    global _client
    if _client is None:
        _client = boto3.client("secretsmanager")
    return _client


def get_webhook_secret() -> bytes:
    """Return the webhook shared secret as bytes for HMAC validation.

    Resolution order:
      1. ``WEBHOOK_SECRET`` env var (dev / test convenience).
      2. Secrets Manager ``webhook_secret`` key.

    Returns ``b""`` when neither source provides a value; the caller
    (webhook validator) will then reject every signature as invalid.
    """
    env_val = os.environ.get("WEBHOOK_SECRET")
    if env_val:
        return env_val.encode("utf-8")
    return _load_secrets().get(_WEBHOOK_KEY, "").encode("utf-8")


def get_github_token() -> str | None:
    """Return the GitHub App / PAT token, or ``None`` if unavailable.

    Resolution order:
      1. ``GITHUB_TOKEN`` env var (dev / test convenience).
      2. Secrets Manager ``github_token`` key.

    Returns ``None`` when neither source provides a value; downstream
    HTTP helpers then omit the ``Authorization`` header, at which point
    the GitHub API will reject the request with 401 and the handler
    surfaces that as a normal :class:`GitHubFetchError` path.
    """
    env_val = os.environ.get("GITHUB_TOKEN")
    if env_val:
        return env_val
    val = _load_secrets().get(_TOKEN_KEY)
    return val if val else None


# ---------------------------------------------------------------------------
# Internal — secret payload loader with defensive parsing
# ---------------------------------------------------------------------------


def _load_secrets() -> dict[str, str]:
    """Fetch and cache the Secrets Manager payload.

    Every failure mode — no ARN configured, transport error, API error,
    non-JSON body, non-dict JSON — is logged and turned into an empty
    dict. The cache is still populated with the empty dict so
    subsequent calls do not retry a failing fetch on every invocation.
    """
    global _cache
    if _cache is not None:
        return _cache

    arn = (os.environ.get("SECRETS_ARN") or "").strip()
    if not arn:
        logger.info("SECRETS_ARN not set; env-only credential mode")
        _cache = {}
        return _cache

    try:
        response = get_secretsmanager_client().get_secret_value(SecretId=arn)
    except (ClientError, BotoCoreError) as e:
        logger.error("Failed to fetch secrets from %s: %s", arn, e)
        _cache = {}
        return _cache

    raw = response.get("SecretString") or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("Secrets Manager payload is not valid JSON: %s", e)
        _cache = {}
        return _cache

    if not isinstance(parsed, dict):
        logger.error(
            "Secrets Manager payload is not a JSON object; got %s",
            type(parsed).__name__,
        )
        _cache = {}
        return _cache

    _cache = {k: str(v) for k, v in parsed.items() if isinstance(v, str)}
    return _cache
