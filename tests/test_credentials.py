"""Tests for :mod:`code_review_agent.credentials`.

Two mocking modes:

  * **moto** for the happy-path Secrets Manager round-trip. Verifies the
    module talks to the real boto3 API surface — no surprises when a
    live Lambda hits Secrets Manager for the first time.
  * **MagicMock** for the failure paths (:class:`ClientError`,
    :class:`BotoCoreError`, malformed JSON, non-dict payloads). moto
    can't easily simulate these, and a direct mock is more precise.

The autouse fixture clears every relevant env var and resets both
module-level singletons around each test so ordering is irrelevant.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from moto import mock_aws

from code_review_agent import credentials

_SECRET_NAME = "code-review-agent/prod/creds"  # noqa: S105 -- moto secret path, not a credential
_WEBHOOK_VALUE = "shared-secret-abcdef"
_TOKEN_VALUE = "ghs_test_installation_token"  # noqa: S105 -- test literal


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_credentials_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset env vars + module-level caches around every test."""
    monkeypatch.delenv("SECRETS_ARN", raising=False)
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    credentials._client = None
    credentials._cache = None
    yield
    credentials._client = None
    credentials._cache = None


@pytest.fixture
def sm_secret(monkeypatch: pytest.MonkeyPatch):
    """Create a real (moto) Secrets Manager secret and point SECRETS_ARN at it."""
    with mock_aws():
        sm = boto3.client("secretsmanager", region_name="us-east-1")
        response = sm.create_secret(
            Name=_SECRET_NAME,
            SecretString=json.dumps(
                {"webhook_secret": _WEBHOOK_VALUE, "github_token": _TOKEN_VALUE}
            ),
        )
        monkeypatch.setenv("SECRETS_ARN", response["ARN"])
        credentials._client = None  # force lazy re-init inside the moto scope
        credentials._cache = None
        yield sm


@pytest.fixture
def stub_sm() -> MagicMock:
    """Install a MagicMock as the module's Secrets Manager client."""
    client = MagicMock(name="secretsmanager")
    credentials._client = client
    return client


# ---------------------------------------------------------------------------
# Env-first resolution
# ---------------------------------------------------------------------------


def test_get_webhook_secret_prefers_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBHOOK_SECRET", "env-webhook")
    assert credentials.get_webhook_secret() == b"env-webhook"


def test_get_github_token_prefers_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "env-token")
    assert credentials.get_github_token() == "env-token"


def test_env_wins_over_secrets_manager(monkeypatch: pytest.MonkeyPatch, sm_secret: Any) -> None:
    """Even when SECRETS_ARN is set, env vars take precedence."""
    monkeypatch.setenv("WEBHOOK_SECRET", "env-wins-webhook")
    monkeypatch.setenv("GITHUB_TOKEN", "env-wins-token")

    assert credentials.get_webhook_secret() == b"env-wins-webhook"
    assert credentials.get_github_token() == "env-wins-token"


def test_no_env_no_arn_returns_empty_and_none() -> None:
    assert credentials.get_webhook_secret() == b""
    assert credentials.get_github_token() is None


def test_empty_env_falls_through_to_secrets_manager(
    monkeypatch: pytest.MonkeyPatch, sm_secret: Any
) -> None:
    """An empty env var is treated as unset, not as an explicit override."""
    monkeypatch.setenv("WEBHOOK_SECRET", "")
    monkeypatch.setenv("GITHUB_TOKEN", "")

    assert credentials.get_webhook_secret() == _WEBHOOK_VALUE.encode("utf-8")
    assert credentials.get_github_token() == _TOKEN_VALUE


# ---------------------------------------------------------------------------
# Secrets Manager happy path (moto)
# ---------------------------------------------------------------------------


def test_get_webhook_secret_from_secrets_manager(sm_secret: Any) -> None:
    assert credentials.get_webhook_secret() == _WEBHOOK_VALUE.encode("utf-8")


def test_get_github_token_from_secrets_manager(sm_secret: Any) -> None:
    assert credentials.get_github_token() == _TOKEN_VALUE


def test_secret_payload_missing_key_returns_absent_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial payload (only webhook_secret) is legal during token rotation."""
    with mock_aws():
        sm = boto3.client("secretsmanager", region_name="us-east-1")
        response = sm.create_secret(
            Name=_SECRET_NAME,
            SecretString=json.dumps({"webhook_secret": _WEBHOOK_VALUE}),
        )
        monkeypatch.setenv("SECRETS_ARN", response["ARN"])
        credentials._client = None
        credentials._cache = None

        assert credentials.get_webhook_secret() == _WEBHOOK_VALUE.encode("utf-8")
        assert credentials.get_github_token() is None


def test_non_string_values_in_payload_are_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only string-valued keys survive the coercion pass."""
    with mock_aws():
        sm = boto3.client("secretsmanager", region_name="us-east-1")
        response = sm.create_secret(
            Name=_SECRET_NAME,
            SecretString=json.dumps(
                {
                    "webhook_secret": _WEBHOOK_VALUE,
                    "github_token": 12345,  # not a string
                    "extra": ["ignored"],
                }
            ),
        )
        monkeypatch.setenv("SECRETS_ARN", response["ARN"])
        credentials._client = None
        credentials._cache = None

        assert credentials.get_webhook_secret() == _WEBHOOK_VALUE.encode("utf-8")
        assert credentials.get_github_token() is None


# ---------------------------------------------------------------------------
# Secrets Manager failure paths (MagicMock)
# ---------------------------------------------------------------------------


def test_client_error_returns_empty_and_none(
    monkeypatch: pytest.MonkeyPatch, stub_sm: MagicMock
) -> None:
    monkeypatch.setenv("SECRETS_ARN", "arn:aws:secretsmanager:us-east-1:123:secret:x")
    stub_sm.get_secret_value.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
        "GetSecretValue",
    )

    assert credentials.get_webhook_secret() == b""
    assert credentials.get_github_token() is None


def test_transport_error_returns_empty_and_none(
    monkeypatch: pytest.MonkeyPatch, stub_sm: MagicMock
) -> None:
    monkeypatch.setenv("SECRETS_ARN", "arn:aws:secretsmanager:us-east-1:123:secret:x")
    stub_sm.get_secret_value.side_effect = EndpointConnectionError(
        endpoint_url="https://secretsmanager.us-east-1.amazonaws.com/"
    )

    assert credentials.get_webhook_secret() == b""
    assert credentials.get_github_token() is None


def test_non_json_payload_returns_empty_and_none(
    monkeypatch: pytest.MonkeyPatch, stub_sm: MagicMock
) -> None:
    monkeypatch.setenv("SECRETS_ARN", "arn:aws:secretsmanager:us-east-1:123:secret:x")
    stub_sm.get_secret_value.return_value = {"SecretString": "not json !!!"}

    assert credentials.get_webhook_secret() == b""
    assert credentials.get_github_token() is None


def test_non_dict_json_payload_returns_empty_and_none(
    monkeypatch: pytest.MonkeyPatch, stub_sm: MagicMock
) -> None:
    monkeypatch.setenv("SECRETS_ARN", "arn:aws:secretsmanager:us-east-1:123:secret:x")
    stub_sm.get_secret_value.return_value = {"SecretString": json.dumps(["a", "b", "c"])}

    assert credentials.get_webhook_secret() == b""
    assert credentials.get_github_token() is None


def test_missing_secret_string_key_returns_empty_and_none(
    monkeypatch: pytest.MonkeyPatch, stub_sm: MagicMock
) -> None:
    """Some Secrets Manager responses omit SecretString (binary secrets)."""
    monkeypatch.setenv("SECRETS_ARN", "arn:aws:secretsmanager:us-east-1:123:secret:x")
    stub_sm.get_secret_value.return_value = {}

    assert credentials.get_webhook_secret() == b""
    assert credentials.get_github_token() is None


# ---------------------------------------------------------------------------
# Caching semantics
# ---------------------------------------------------------------------------


def test_secrets_are_fetched_exactly_once_per_container(
    monkeypatch: pytest.MonkeyPatch, stub_sm: MagicMock
) -> None:
    """Warm invocations must not re-hit Secrets Manager."""
    monkeypatch.setenv("SECRETS_ARN", "arn:aws:secretsmanager:us-east-1:123:secret:x")
    stub_sm.get_secret_value.return_value = {
        "SecretString": json.dumps({"webhook_secret": _WEBHOOK_VALUE, "github_token": _TOKEN_VALUE})
    }

    for _ in range(5):
        assert credentials.get_webhook_secret() == _WEBHOOK_VALUE.encode("utf-8")
        assert credentials.get_github_token() == _TOKEN_VALUE

    assert stub_sm.get_secret_value.call_count == 1


def test_fetch_failure_is_also_cached_no_retry_storm(
    monkeypatch: pytest.MonkeyPatch, stub_sm: MagicMock
) -> None:
    """A failed fetch caches an empty payload so we don't spam Secrets Manager."""
    monkeypatch.setenv("SECRETS_ARN", "arn:aws:secretsmanager:us-east-1:123:secret:x")
    stub_sm.get_secret_value.side_effect = ClientError(
        {"Error": {"Code": "InternalServiceError", "Message": "oops"}},
        "GetSecretValue",
    )

    for _ in range(5):
        assert credentials.get_webhook_secret() == b""
        assert credentials.get_github_token() is None

    assert stub_sm.get_secret_value.call_count == 1


# ---------------------------------------------------------------------------
# Client singleton
# ---------------------------------------------------------------------------


def test_secretsmanager_client_is_cached_singleton() -> None:
    with patch("code_review_agent.credentials.boto3") as mock_boto3:
        mock_boto3.client.return_value = MagicMock(name="sm")
        credentials._client = None

        first = credentials.get_secretsmanager_client()
        second = credentials.get_secretsmanager_client()

        assert first is second
        assert mock_boto3.client.call_count == 1
        assert mock_boto3.client.call_args.args == ("secretsmanager",)
