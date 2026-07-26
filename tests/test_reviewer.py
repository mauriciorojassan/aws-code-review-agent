"""Tests for the Bedrock reviewer module.

Mocking is self-contained here: every test either installs a
:class:`unittest.mock.MagicMock` as the module-level Bedrock client via the
``stub_client`` fixture, or patches :mod:`boto3` on the reviewer module to
count constructor invocations. No real AWS calls are ever made and no
``conftest`` infrastructure is required by these tests specifically.

The autouse fixture ``_reset_reviewer_state`` clears the ``BEDROCK_MODEL_ID``
env var and the module-level lazy singleton before **and after** each test
so that ordering is irrelevant.
"""

from __future__ import annotations

import io
import json
import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import BotoCoreError, ClientError

from code_review_agent import reviewer
from code_review_agent.models import Finding

_HAIKU_DEFAULT = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


# ---------------------------------------------------------------------------
# Helpers and fixtures
# ---------------------------------------------------------------------------


def _fake_body(payload: Any) -> io.BytesIO:
    """Wrap a Python payload as a Bedrock-style ``body`` stream."""
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


def _wrap_findings(findings: list[dict]) -> dict:
    """Wrap ``findings`` as a Bedrock ``content[0].text`` response body."""
    return {"content": [{"text": json.dumps(findings)}]}


def _one_finding_response() -> dict:
    """Return an ``invoke_model`` response containing exactly one finding."""
    return {
        "body": _fake_body(
            _wrap_findings(
                [
                    {
                        "file": "src/main.py",
                        "line": 3,
                        "severity": "warning",
                        "message": "possible bug",
                        "suggestion": "add a guard",
                    }
                ]
            )
        )
    }


@pytest.fixture(autouse=True)
def _reset_reviewer_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset env var and module-level singleton around every test."""
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    reviewer._client = None
    yield
    reviewer._client = None


@pytest.fixture
def stub_client() -> MagicMock:
    """Install a ``MagicMock`` as the reviewer module's Bedrock client."""
    client = MagicMock(name="bedrock-client")
    reviewer._client = client
    return client


# ---------------------------------------------------------------------------
# Haiku model_id validation (business rule 1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model_id",
    [
        "anthropic.claude-3-haiku-20240307",
        "anthropic.claude-3-haiku-latest",
        "anthropic.claude-3-5-haiku-20241022",
        "anthropic.claude-3-5-haiku-v1:0",
        "anthropic.claude-3-7-haiku-20250101",
        # Haiku 4.5 family — foundation form and cross-region inference profiles.
        "anthropic.claude-haiku-4-5-20251001-v1:0",
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    ],
)
def test_analyze_diff_accepts_haiku_variants(stub_client: MagicMock, model_id: str) -> None:
    """All documented Haiku family variants must be accepted."""
    stub_client.invoke_model.return_value = _one_finding_response()
    findings = reviewer.analyze_diff("diff", model_id=model_id)
    assert len(findings) == 1
    assert stub_client.invoke_model.call_args.kwargs["modelId"] == model_id


@pytest.mark.parametrize(
    "model_id",
    [
        "anthropic.claude-3-sonnet-20240229",
        "anthropic.claude-3-5-sonnet-20241022",
        "anthropic.claude-3-opus-20240229",
        "anthropic.claude-3-5-opus-20250101",
        "meta.llama3-8b-instruct-v1:0",
        "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-haiku",
        "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0",
        # Synthetic non-existent Haiku id — real Claude Haiku 4.5 uses the
        # ``claude-haiku-4-5`` notch; ``claude-4-haiku`` never existed.
        "anthropic.claude-4-haiku-20260101",
    ],
)
def test_analyze_diff_rejects_non_haiku(stub_client: MagicMock, model_id: str) -> None:
    """Sonnet, Opus, non-anthropic ids and ARN-embedded ids are rejected."""
    with pytest.raises(ValueError, match="Haiku"):
        reviewer.analyze_diff("diff", model_id=model_id)
    stub_client.invoke_model.assert_not_called()


@pytest.mark.parametrize("bad", [None, "", "   ", "\t\n"])
def test_analyze_diff_rejects_empty_none_whitespace(stub_client: MagicMock, bad: Any) -> None:
    """Explicit ``None`` / empty / whitespace-only ids raise ``ValueError``."""
    with pytest.raises(ValueError, match="non-empty Claude Haiku"):
        reviewer.analyze_diff("diff", model_id=bad)
    stub_client.invoke_model.assert_not_called()


def test_analyze_diff_rejects_non_string_type(stub_client: MagicMock) -> None:
    """Non-string types (int, list, dict) are rejected before invocation."""
    with pytest.raises(ValueError, match="non-empty Claude Haiku"):
        reviewer.analyze_diff("diff", model_id=12345)  # type: ignore[arg-type]
    stub_client.invoke_model.assert_not_called()


# ---------------------------------------------------------------------------
# Env var override (business rule 2)
# ---------------------------------------------------------------------------


def test_env_var_overrides_default(monkeypatch: pytest.MonkeyPatch, stub_client: MagicMock) -> None:
    """``BEDROCK_MODEL_ID`` env var wins when ``model_id`` is omitted."""
    override = "anthropic.claude-3-5-haiku-20241022"
    monkeypatch.setenv("BEDROCK_MODEL_ID", override)
    stub_client.invoke_model.return_value = _one_finding_response()

    reviewer.analyze_diff("diff")

    assert stub_client.invoke_model.call_args.kwargs["modelId"] == override


def test_default_used_when_env_var_unset(stub_client: MagicMock) -> None:
    """With no env var and no argument, the documented default is used."""
    stub_client.invoke_model.return_value = _one_finding_response()

    reviewer.analyze_diff("diff")

    assert stub_client.invoke_model.call_args.kwargs["modelId"] == _HAIKU_DEFAULT


def test_env_var_empty_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, stub_client: MagicMock
) -> None:
    """An empty env var is treated as unset, not as an invalid value."""
    monkeypatch.setenv("BEDROCK_MODEL_ID", "")
    stub_client.invoke_model.return_value = _one_finding_response()

    reviewer.analyze_diff("diff")

    assert stub_client.invoke_model.call_args.kwargs["modelId"] == _HAIKU_DEFAULT


def test_env_var_non_haiku_rejected(
    monkeypatch: pytest.MonkeyPatch, stub_client: MagicMock
) -> None:
    """A non-Haiku env var override still fails validation."""
    monkeypatch.setenv("BEDROCK_MODEL_ID", "anthropic.claude-3-opus-20240229")
    with pytest.raises(ValueError, match="Haiku"):
        reviewer.analyze_diff("diff")
    stub_client.invoke_model.assert_not_called()


def test_explicit_model_id_beats_env_var(
    monkeypatch: pytest.MonkeyPatch, stub_client: MagicMock
) -> None:
    """When both env var and argument are set, the argument wins."""
    monkeypatch.setenv("BEDROCK_MODEL_ID", "anthropic.claude-3-5-haiku-20241022")
    stub_client.invoke_model.return_value = _one_finding_response()

    reviewer.analyze_diff("diff", model_id="anthropic.claude-3-haiku-latest")

    assert stub_client.invoke_model.call_args.kwargs["modelId"] == "anthropic.claude-3-haiku-latest"


# ---------------------------------------------------------------------------
# Bedrock client singleton (business rule 3)
# ---------------------------------------------------------------------------


def test_bedrock_client_is_cached_singleton() -> None:
    """``boto3.client`` must be constructed at most once across many calls."""
    with patch("code_review_agent.reviewer.boto3") as mock_boto3:
        client = MagicMock(name="bedrock-client")
        # side_effect returns a fresh body stream per invocation because
        # BytesIO objects are single-consumption.
        client.invoke_model.side_effect = lambda **_: _one_finding_response()
        mock_boto3.client.return_value = client

        reviewer._client = None  # force lazy init from scratch
        for _ in range(5):
            reviewer.analyze_diff("diff", model_id=_HAIKU_DEFAULT)

        assert mock_boto3.client.call_count == 1
        assert mock_boto3.client.call_args.args == ("bedrock-runtime",)
        assert client.invoke_model.call_count == 5


def test_bedrock_client_is_configured_with_lambda_safe_timeouts() -> None:
    """The Bedrock client must be built with timeouts that fit inside 30s Lambda budget.

    Regression guard: without an explicit ``botocore.config.Config``, boto3
    defaults to a 60-second read timeout, which means a hanging Bedrock
    call is killed by Lambda's own timeout (surfacing as an opaque
    ``Task timed out`` in CloudWatch) rather than by a catchable
    ``ReadTimeoutError`` we can log with context.
    """
    with patch("code_review_agent.reviewer.boto3") as mock_boto3:
        mock_boto3.client.return_value = MagicMock(name="bedrock-client")
        reviewer._client = None
        reviewer.get_bedrock_client()

        kwargs = mock_boto3.client.call_args.kwargs
        assert "config" in kwargs, "Bedrock client must be built with a botocore Config"
        cfg = kwargs["config"]
        assert cfg.read_timeout <= 25
        assert cfg.connect_timeout <= 5
        # max_attempts=1 means "no retries" — one attempt total.
        assert cfg.retries["max_attempts"] == 1


def test_get_bedrock_client_lazily_constructs() -> None:
    """First ``get_bedrock_client()`` call creates the client; second reuses."""
    with patch("code_review_agent.reviewer.boto3") as mock_boto3:
        mock_boto3.client.return_value = MagicMock(name="bedrock-client")

        reviewer._client = None
        first = reviewer.get_bedrock_client()
        second = reviewer.get_bedrock_client()

        assert first is second
        assert mock_boto3.client.call_count == 1


# ---------------------------------------------------------------------------
# Bedrock error propagation
# ---------------------------------------------------------------------------


def test_analyze_diff_propagates_bedrock_client_error(stub_client: MagicMock) -> None:
    """AWS API errors from Bedrock must surface to the handler.

    ``analyze_diff`` intentionally does not swallow :class:`ClientError`
    (throttling, access denied, model not found, service unavailable) —
    the design contract (``design.md`` §1 step 9) puts responsibility on
    the Lambda handler to catch these and translate them into a 500 so
    GitHub retries the webhook.
    """
    stub_client.invoke_model.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "Too many requests"}},
        "InvokeModel",
    )
    with pytest.raises(ClientError, match="ThrottlingException"):
        reviewer.analyze_diff("diff", model_id=_HAIKU_DEFAULT)


def test_analyze_diff_propagates_bedrock_botocore_error(stub_client: MagicMock) -> None:
    """Transport-level ``BotoCoreError`` subclasses must also propagate."""
    from botocore.exceptions import EndpointConnectionError

    stub_client.invoke_model.side_effect = EndpointConnectionError(
        endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com/"
    )
    with pytest.raises(BotoCoreError):
        reviewer.analyze_diff("diff", model_id=_HAIKU_DEFAULT)


# ---------------------------------------------------------------------------
# Response parsing — happy path
# ---------------------------------------------------------------------------


def test_parse_single_finding(stub_client: MagicMock) -> None:
    stub_client.invoke_model.return_value = _one_finding_response()

    findings = reviewer.analyze_diff("diff", model_id=_HAIKU_DEFAULT)

    assert len(findings) == 1
    assert isinstance(findings[0], Finding)
    assert findings[0].file == "src/main.py"
    assert findings[0].severity == "warning"


def test_parse_multiple_findings(stub_client: MagicMock) -> None:
    stub_client.invoke_model.return_value = {
        "body": _fake_body(
            _wrap_findings(
                [
                    {
                        "file": "a.py",
                        "line": 1,
                        "severity": "info",
                        "message": "m",
                        "suggestion": None,
                    },
                    {
                        "file": "b.py",
                        "line": 2,
                        "severity": "warning",
                        "message": "n",
                        "suggestion": "fix",
                    },
                    {
                        "file": "c.py",
                        "line": 3,
                        "severity": "error",
                        "message": "o",
                    },
                ]
            )
        )
    }

    findings = reviewer.analyze_diff("diff", model_id=_HAIKU_DEFAULT)

    assert [f.file for f in findings] == ["a.py", "b.py", "c.py"]
    assert findings[2].suggestion is None  # default when omitted


def test_parse_empty_array(stub_client: MagicMock) -> None:
    stub_client.invoke_model.return_value = {"body": _fake_body(_wrap_findings([]))}

    findings = reviewer.analyze_diff("diff", model_id=_HAIKU_DEFAULT)

    assert findings == []


# ---------------------------------------------------------------------------
# Response parsing — robustness (business rule 4)
# ---------------------------------------------------------------------------


def _assert_parse_empty_with_warning(
    stub_client: MagicMock, body_payload: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Run ``analyze_diff`` against a malformed body; expect [] + a warning."""
    stub_client.invoke_model.return_value = {"body": _fake_body(body_payload)}
    with caplog.at_level(logging.WARNING, logger="code_review_agent.reviewer"):
        result = reviewer.analyze_diff("diff", model_id=_HAIKU_DEFAULT)
    assert result == []
    assert caplog.records, "expected at least one warning log record"


def test_parse_missing_content_key(
    stub_client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    _assert_parse_empty_with_warning(stub_client, {}, caplog)


def test_parse_content_not_a_list(stub_client: MagicMock, caplog: pytest.LogCaptureFixture) -> None:
    _assert_parse_empty_with_warning(stub_client, {"content": "oops"}, caplog)


def test_parse_content_empty_list(stub_client: MagicMock, caplog: pytest.LogCaptureFixture) -> None:
    _assert_parse_empty_with_warning(stub_client, {"content": []}, caplog)


def test_parse_content_first_not_object(
    stub_client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    _assert_parse_empty_with_warning(stub_client, {"content": ["oops"]}, caplog)


def test_parse_missing_text_key(stub_client: MagicMock, caplog: pytest.LogCaptureFixture) -> None:
    _assert_parse_empty_with_warning(stub_client, {"content": [{}]}, caplog)


def test_parse_empty_text(stub_client: MagicMock, caplog: pytest.LogCaptureFixture) -> None:
    _assert_parse_empty_with_warning(stub_client, {"content": [{"text": ""}]}, caplog)


def test_parse_non_string_text(stub_client: MagicMock, caplog: pytest.LogCaptureFixture) -> None:
    _assert_parse_empty_with_warning(stub_client, {"content": [{"text": 42}]}, caplog)


def test_parse_text_not_json(stub_client: MagicMock, caplog: pytest.LogCaptureFixture) -> None:
    _assert_parse_empty_with_warning(stub_client, {"content": [{"text": "not json !!! {"}]}, caplog)


def test_parse_text_json_but_not_array(
    stub_client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    _assert_parse_empty_with_warning(
        stub_client,
        {"content": [{"text": json.dumps({"findings": []})}]},
        caplog,
    )


def test_parse_text_json_scalar(stub_client: MagicMock, caplog: pytest.LogCaptureFixture) -> None:
    _assert_parse_empty_with_warning(stub_client, {"content": [{"text": json.dumps(42)}]}, caplog)


def test_parse_invalid_element_skipped_valid_kept(
    stub_client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """Individual invalid findings are skipped; valid ones are preserved."""
    valid = {
        "file": "a.py",
        "line": 5,
        "severity": "warning",
        "message": "m",
    }
    invalid_schema = {
        "file": "b.py",
        "line": "not-an-int",
        "severity": "warning",
        "message": "m",
    }
    invalid_severity = {
        "file": "c.py",
        "line": 5,
        "severity": "critical",
        "message": "m",
    }
    stub_client.invoke_model.return_value = {
        "body": _fake_body(
            {
                "content": [
                    {"text": json.dumps([valid, invalid_schema, invalid_severity, "just a string"])}
                ]
            }
        )
    }

    with caplog.at_level(logging.WARNING, logger="code_review_agent.reviewer"):
        findings = reviewer.analyze_diff("diff", model_id=_HAIKU_DEFAULT)

    assert len(findings) == 1
    assert findings[0].file == "a.py"
    skip_records = [r for r in caplog.records if "Skipping" in r.getMessage()]
    assert len(skip_records) == 3


def test_parse_response_body_not_a_dict() -> None:
    """Called directly with non-dict inputs, ``_parse_findings`` returns []."""
    assert reviewer._parse_findings([]) == []
    assert reviewer._parse_findings("nope") == []
    assert reviewer._parse_findings(None) == []
    assert reviewer._parse_findings(42) == []


def test_parse_never_raises_on_arbitrary_garbage(stub_client: MagicMock) -> None:
    """Fuzzy sanity check: no shape of ``response_body`` propagates an error."""
    garbage_bodies: list[Any] = [
        {"content": None},
        {"content": [None]},
        {"content": [{"text": None}]},
        {"content": [{"text": "[not really json"}]},
        {"content": [{"text": json.dumps([None, 1, "x"])}]},
        {"content": [{"text": json.dumps([{"file": "a"}])}]},  # missing required
    ]
    for body in garbage_bodies:
        stub_client.invoke_model.return_value = {"body": _fake_body(body)}
        assert reviewer.analyze_diff("diff", model_id=_HAIKU_DEFAULT) == []


# ---------------------------------------------------------------------------
# Prompt construction & invocation body
# ---------------------------------------------------------------------------


def test_system_prompt_defines_reviewer_persona() -> None:
    sp = reviewer._system_prompt()
    assert "code reviewer" in sp.lower()
    assert "json" in sp.lower()
    assert "severity" in sp.lower()


def test_build_review_prompt_embeds_diff_fenced() -> None:
    prompt = reviewer._build_review_prompt("--- a\n+++ b\n")
    assert "```diff" in prompt
    assert "--- a" in prompt


def test_invoke_model_body_shape(stub_client: MagicMock) -> None:
    """The body sent to Bedrock must be Anthropic-shaped and carry the diff."""
    stub_client.invoke_model.return_value = _one_finding_response()

    reviewer.analyze_diff("some diff content", model_id=_HAIKU_DEFAULT)

    kwargs = stub_client.invoke_model.call_args.kwargs
    assert kwargs["contentType"] == "application/json"
    assert kwargs["accept"] == "application/json"
    body = json.loads(kwargs["body"])
    assert body["anthropic_version"] == "bedrock-2023-05-31"
    assert body["max_tokens"] == 4096
    assert body["messages"][0]["role"] == "user"
    assert "some diff content" in body["messages"][0]["content"]
    assert body["system"] == reviewer._system_prompt()
