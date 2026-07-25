"""Structured logging and CloudWatch custom metrics.

This module owns the observability boundary described in ``design.md`` §8.

Design decisions:
  * **Logs use Python's ``logging`` module**, not the CloudWatch Logs API.
    In a Lambda runtime, records written to a module logger propagate to
    the root handler and are captured on stdout by the runtime, which
    ships them to CloudWatch Logs without any explicit ``put_log_events``
    call. This is the AWS-recommended pattern and it keeps the code path
    fast (no sequence-token juggling) and easy to test with ``caplog``.
  * **Metrics use ``boto3.client("cloudwatch").put_metric_data``** with a
    lazy singleton client that mirrors :mod:`diff_cache` and
    :mod:`reviewer`. Metric emission is best-effort: a ``ClientError``
    is logged at warning level and swallowed so that a CloudWatch outage
    can never fail an otherwise successful review.
  * **Timestamps are ISO 8601 UTC** with second precision and a trailing
    ``Z``, matching the log-schema example in ``design.md`` §8.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_DEFAULT_NAMESPACE = "CodeReviewAgent"

# Module-level lazy singleton. Reset to ``None`` in tests to force a fresh
# client construction under a moto or MagicMock scope.
_client: Any | None = None


def _namespace() -> str:
    """Return the CloudWatch metric namespace, honoring env override."""
    return os.environ.get("METRICS_NAMESPACE") or _DEFAULT_NAMESPACE


def _now_iso() -> str:
    """Return current UTC time as an ISO 8601 second-precision string with 'Z' suffix."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_cloudwatch_client() -> Any:
    """Return the module-level CloudWatch client, creating it lazily."""
    global _client
    if _client is None:
        _client = boto3.client("cloudwatch")
    return _client


def emit_structured_log(
    event: str,
    pr_url: str,
    repo: str,
    action: str,
    head_sha: str,
    review_id: str | None,
    status: str,
    **kwargs: Any,
) -> None:
    """Emit one JSON-encoded pipeline event to CloudWatch Logs.

    The seven positional/keyword fields are the required schema per NFR
    and ``design.md`` §8. Additional context — severity counts, excluded
    file totals, truncation flags, timing data — is passed via
    ``**kwargs`` and merged into the same JSON object; keys colliding
    with the required fields are overwritten by the required values so
    the schema contract cannot be silently broken by a caller.

    Args:
        event: Event name (e.g. ``"pr_review_completed"``,
            ``"pr_review_out_of_hunk"``).
        pr_url: Full GitHub PR URL.
        repo: ``owner/name`` slug.
        action: GitHub webhook action (``"opened"``, ``"synchronize"``).
        head_sha: Head commit SHA of the PR.
        review_id: GitHub review id when a review was posted; ``None``
            for no-op / skip / failure outcomes.
        status: Terminal outcome (``"success"``, ``"skipped"``,
            ``"failed"``, ...).
        **kwargs: Arbitrary extra fields merged into the log record.
    """
    payload: dict[str, Any] = dict(kwargs)
    payload.update(
        {
            "event": event,
            "pr_url": pr_url,
            "repo": repo,
            "action": action,
            "head_sha": head_sha,
            "review_id": review_id,
            "status": status,
            "timestamp": _now_iso(),
        }
    )
    logger.info(json.dumps(payload, default=str, sort_keys=False))


def emit_metric(
    metric_name: str,
    value: float,
    dimensions: dict[str, Any],
) -> None:
    """Emit a single CloudWatch custom metric under the configured namespace.

    Failures are logged and swallowed: observability must never propagate
    an exception into the pipeline. ``dimensions`` values are coerced to
    strings because CloudWatch requires string dimension values.

    Args:
        metric_name: CloudWatch metric name (e.g. ``"ReviewCompleted"``,
            ``"ReviewFailed"``).
        value: Metric value; typically ``1`` for count-style metrics.
        dimensions: Mapping of dimension name → value. Non-string values
            are ``str()``-coerced.
    """
    metric_data = [
        {
            "MetricName": metric_name,
            "Value": float(value),
            "Unit": "Count",
            "Dimensions": [{"Name": str(k), "Value": str(v)} for k, v in dimensions.items()],
        }
    ]
    client = get_cloudwatch_client()
    try:
        client.put_metric_data(Namespace=_namespace(), MetricData=metric_data)
    except ClientError as e:
        logger.warning("Failed to emit metric %s: %s", metric_name, e)
