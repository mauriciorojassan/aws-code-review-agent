"""Tests for :mod:`code_review_agent.observability`.

Two mocking strategies coexist here:

* **caplog** for :func:`emit_structured_log` — the record is captured on
  the ``code_review_agent.observability`` logger and its ``message`` is a
  JSON string that we round-trip through :func:`json.loads` to assert the
  schema.
* **moto** + **unittest.mock** for :func:`emit_metric` — moto gives us a
  real-shape ``list_metrics`` verification path, and a targeted MagicMock
  lets us assert exact call kwargs and drive the ``ClientError`` swallow
  path deterministically.

The autouse ``_reset_observability_state`` fixture clears the module-level
``_client`` singleton and the ``METRICS_NAMESPACE`` env var so ordering is
irrelevant.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from moto import mock_aws

from code_review_agent import observability

_LOGGER_NAME = "code_review_agent.observability"
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_observability_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset singleton client and env var before and after each test."""
    monkeypatch.delenv("METRICS_NAMESPACE", raising=False)
    observability._client = None
    yield
    observability._client = None


@pytest.fixture
def cw_client():
    """Real (moto-backed) CloudWatch client for cross-checking metric state."""
    with mock_aws():
        client = boto3.client("cloudwatch", region_name="us-east-1")
        observability._client = None  # force lazy re-init inside the moto scope
        yield client


@pytest.fixture
def stub_cw() -> MagicMock:
    """Install a MagicMock as the module's CloudWatch client."""
    client = MagicMock(name="cloudwatch")
    observability._client = client
    return client


def _capture_log_payload(caplog: pytest.LogCaptureFixture) -> dict[str, Any]:
    """Return the JSON payload from the single INFO record on the module logger."""
    records = [r for r in caplog.records if r.name == _LOGGER_NAME and r.levelno == logging.INFO]
    assert len(records) == 1, f"expected exactly one INFO record; got {len(records)}"
    return json.loads(records[0].getMessage())


# ---------------------------------------------------------------------------
# emit_structured_log
# ---------------------------------------------------------------------------


def test_structured_log_writes_all_required_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        observability.emit_structured_log(
            event="pr_review_completed",
            pr_url="https://github.com/owner/repo/pull/42",
            repo="owner/repo",
            action="opened",
            head_sha="abc123",
            review_id="99",
            status="success",
        )

    payload = _capture_log_payload(caplog)
    assert payload["event"] == "pr_review_completed"
    assert payload["pr_url"] == "https://github.com/owner/repo/pull/42"
    assert payload["repo"] == "owner/repo"
    assert payload["action"] == "opened"
    assert payload["head_sha"] == "abc123"
    assert payload["review_id"] == "99"
    assert payload["status"] == "success"
    assert "timestamp" in payload


def test_structured_log_timestamp_matches_iso8601_utc(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        observability.emit_structured_log(
            event="e",
            pr_url="u",
            repo="r",
            action="opened",
            head_sha="s",
            review_id=None,
            status="success",
        )
    payload = _capture_log_payload(caplog)
    assert _ISO_RE.match(payload["timestamp"]), payload["timestamp"]


def test_structured_log_review_id_none_is_json_null(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        observability.emit_structured_log(
            event="e",
            pr_url="u",
            repo="r",
            action="opened",
            head_sha="s",
            review_id=None,
            status="skipped",
        )
    records = [r for r in caplog.records if r.name == _LOGGER_NAME]
    # Raw JSON must contain the literal null for review_id, not the string "None".
    assert '"review_id": null' in records[0].getMessage()


def test_structured_log_accepts_extra_kwargs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        observability.emit_structured_log(
            event="pr_review_completed",
            pr_url="u",
            repo="r",
            action="opened",
            head_sha="s",
            review_id="1",
            status="success",
            severity_counts={"error": 2, "warning": 5, "info": 3},
            excluded_files=3,
            truncated=False,
        )
    payload = _capture_log_payload(caplog)
    assert payload["severity_counts"] == {"error": 2, "warning": 5, "info": 3}
    assert payload["excluded_files"] == 3
    assert payload["truncated"] is False


def test_structured_log_required_field_wins_over_kwargs_collision(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A caller cannot forge ``timestamp`` via kwargs — the synthesized value wins.

    The other required fields (``event``, ``status``, etc.) are formal
    parameters, so Python's argument binding already blocks positional /
    kwargs collisions at call time. Only ``timestamp`` reaches the
    ``**kwargs`` bag, so this is the one field the merge-order guarantee
    actually protects.
    """
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        observability.emit_structured_log(
            event="e",
            pr_url="u",
            repo="r",
            action="opened",
            head_sha="s",
            review_id="1",
            status="success",
            timestamp="1970-01-01T00:00:00Z",  # caller tries to spoof
        )
    payload = _capture_log_payload(caplog)
    assert payload["timestamp"] != "1970-01-01T00:00:00Z"
    assert _ISO_RE.match(payload["timestamp"])


def test_structured_log_output_is_valid_json(caplog: pytest.LogCaptureFixture) -> None:
    """The single log message must parse cleanly as JSON in one go."""
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        observability.emit_structured_log(
            event="e",
            pr_url="u",
            repo="r",
            action="opened",
            head_sha="s",
            review_id="1",
            status="success",
            extra_object={"nested": [1, 2, 3]},
        )
    records = [r for r in caplog.records if r.name == _LOGGER_NAME]
    parsed = json.loads(records[0].getMessage())  # would raise on bad JSON
    assert parsed["extra_object"] == {"nested": [1, 2, 3]}


def test_structured_log_non_json_serializable_kwargs_coerced_via_default_str(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Objects like ``set`` are serialized via ``str()`` rather than crashing."""

    class _Weird:
        def __str__(self) -> str:
            return "weird-thing"

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        observability.emit_structured_log(
            event="e",
            pr_url="u",
            repo="r",
            action="opened",
            head_sha="s",
            review_id="1",
            status="success",
            odd=_Weird(),
        )
    payload = _capture_log_payload(caplog)
    assert payload["odd"] == "weird-thing"


# ---------------------------------------------------------------------------
# emit_metric — moto end-to-end
# ---------------------------------------------------------------------------


def test_emit_metric_review_completed_visible_via_list_metrics(
    cw_client: Any,
) -> None:
    observability.emit_metric(
        "ReviewCompleted",
        1,
        {"repo": "owner/repo", "severity_count": "8"},
    )

    metrics = cw_client.list_metrics(Namespace="CodeReviewAgent")["Metrics"]
    names = {m["MetricName"] for m in metrics}
    assert "ReviewCompleted" in names

    completed = next(m for m in metrics if m["MetricName"] == "ReviewCompleted")
    dim_map = {d["Name"]: d["Value"] for d in completed["Dimensions"]}
    assert dim_map == {"repo": "owner/repo", "severity_count": "8"}


def test_emit_metric_review_failed_visible_via_list_metrics(
    cw_client: Any,
) -> None:
    observability.emit_metric(
        "ReviewFailed",
        1,
        {"repo": "owner/repo", "reason": "rate_limit"},
    )

    metrics = cw_client.list_metrics(Namespace="CodeReviewAgent")["Metrics"]
    failed = [m for m in metrics if m["MetricName"] == "ReviewFailed"]
    assert len(failed) == 1
    dim_map = {d["Name"]: d["Value"] for d in failed[0]["Dimensions"]}
    assert dim_map == {"repo": "owner/repo", "reason": "rate_limit"}


def test_emit_metric_custom_namespace_env_var(
    monkeypatch: pytest.MonkeyPatch, cw_client: Any
) -> None:
    monkeypatch.setenv("METRICS_NAMESPACE", "TeamAgent/Custom")

    observability.emit_metric("ReviewCompleted", 1, {"repo": "o/r"})

    # Custom namespace must contain the metric; default namespace must not.
    custom = cw_client.list_metrics(Namespace="TeamAgent/Custom")["Metrics"]
    default = cw_client.list_metrics(Namespace="CodeReviewAgent")["Metrics"]
    assert any(m["MetricName"] == "ReviewCompleted" for m in custom)
    assert not any(m["MetricName"] == "ReviewCompleted" for m in default)


# ---------------------------------------------------------------------------
# emit_metric — exact call arg assertions via MagicMock
# ---------------------------------------------------------------------------


def test_emit_metric_call_kwargs_shape(stub_cw: MagicMock) -> None:
    observability.emit_metric(
        "ReviewCompleted",
        1,
        {"repo": "owner/repo", "severity_count": 5},
    )

    stub_cw.put_metric_data.assert_called_once()
    kwargs = stub_cw.put_metric_data.call_args.kwargs
    assert kwargs["Namespace"] == "CodeReviewAgent"
    assert kwargs["MetricData"] == [
        {
            "MetricName": "ReviewCompleted",
            "Value": 1.0,
            "Unit": "Count",
            "Dimensions": [
                {"Name": "repo", "Value": "owner/repo"},
                {"Name": "severity_count", "Value": "5"},
            ],
        }
    ]


def test_emit_metric_coerces_non_string_dimension_values(
    stub_cw: MagicMock,
) -> None:
    observability.emit_metric(
        "ReviewCompleted",
        1,
        {"count": 42, "flag": True, "ratio": 0.5},
    )
    dims = stub_cw.put_metric_data.call_args.kwargs["MetricData"][0]["Dimensions"]
    dim_map = {d["Name"]: d["Value"] for d in dims}
    assert dim_map == {"count": "42", "flag": "True", "ratio": "0.5"}


def test_emit_metric_accepts_empty_dimensions(stub_cw: MagicMock) -> None:
    observability.emit_metric("ReviewCompleted", 1, {})
    assert stub_cw.put_metric_data.call_args.kwargs["MetricData"][0]["Dimensions"] == []


def test_emit_metric_value_coerced_to_float(stub_cw: MagicMock) -> None:
    observability.emit_metric("ReviewCompleted", 3, {"repo": "o/r"})
    value = stub_cw.put_metric_data.call_args.kwargs["MetricData"][0]["Value"]
    assert isinstance(value, float)
    assert value == 3.0


# ---------------------------------------------------------------------------
# emit_metric — fail-silent on ClientError
# ---------------------------------------------------------------------------


def test_emit_metric_swallows_client_error(
    stub_cw: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    stub_cw.put_metric_data.side_effect = ClientError(
        {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}},
        "PutMetricData",
    )

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        # Must not raise.
        observability.emit_metric("ReviewFailed", 1, {"repo": "o/r"})

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings
    assert "ReviewFailed" in warnings[0].getMessage()


def test_emit_metric_swallows_botocore_transport_error(
    stub_cw: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """Transport-level ``BotoCoreError`` subclasses must also fail silently.

    Regression guard: an earlier revision only caught :class:`ClientError`,
    which left endpoint / DNS / TLS failures propagating into the pipeline
    and violating the fail-silent NFR.
    """
    stub_cw.put_metric_data.side_effect = EndpointConnectionError(
        endpoint_url="https://monitoring.us-east-1.amazonaws.com/"
    )

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        # Must not raise.
        observability.emit_metric("ReviewCompleted", 1, {"repo": "o/r"})

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings
    assert "ReviewCompleted" in warnings[0].getMessage()


def test_emit_metric_propagates_non_botocore_error(stub_cw: MagicMock) -> None:
    """Non-botocore exceptions are intentionally not swallowed.

    ``RuntimeError``, ``TypeError``, ``ValueError`` and similar signal
    caller bugs (bad dimension shapes, missing kwargs), not observability
    outages. Surface them.
    """
    stub_cw.put_metric_data.side_effect = RuntimeError("bug in caller")
    with pytest.raises(RuntimeError, match="bug in caller"):
        observability.emit_metric("ReviewFailed", 1, {"repo": "o/r"})


# ---------------------------------------------------------------------------
# CloudWatch client singleton
# ---------------------------------------------------------------------------


def test_cloudwatch_client_is_cached_singleton() -> None:
    """``boto3.client`` must be constructed at most once across many emissions."""
    with patch("code_review_agent.observability.boto3") as mock_boto3:
        client = MagicMock(name="cw")
        mock_boto3.client.return_value = client

        observability._client = None
        for i in range(5):
            observability.emit_metric("ReviewCompleted", 1, {"i": i})

        assert mock_boto3.client.call_count == 1
        assert mock_boto3.client.call_args.args == ("cloudwatch",)
        assert client.put_metric_data.call_count == 5


def test_get_cloudwatch_client_lazily_constructs() -> None:
    with patch("code_review_agent.observability.boto3") as mock_boto3:
        mock_boto3.client.return_value = MagicMock(name="cw")

        observability._client = None
        first = observability.get_cloudwatch_client()
        second = observability.get_cloudwatch_client()

        assert first is second
        assert mock_boto3.client.call_count == 1
