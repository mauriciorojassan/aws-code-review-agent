"""S3 caching layer for diffs and analysis results."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3
from botocore.exceptions import ClientError

from code_review_agent.models import Finding

logger = logging.getLogger(__name__)


def _bucket_name() -> str:
    """Resolve cache bucket name at call time so tests can monkeypatch env."""
    name = os.environ.get("DIFF_CACHE_BUCKET", "")
    if not name:
        raise RuntimeError("DIFF_CACHE_BUCKET environment variable is not set")
    return name


def get_s3_client() -> Any:
    """Create S3 client."""
    return boto3.client("s3")


def _diff_key(repo: str, pr: int, sha: str) -> str:
    """Build S3 key for a cached diff."""
    return f"diffs/{repo}/{pr}/{sha}.diff"


def _analysis_key(repo: str, pr: int, sha: str) -> str:
    """Build S3 key for a cached analysis."""
    return f"analyses/{repo}/{pr}/{sha}.json"


def get_cached_diff(repo: str, pr: int, sha: str) -> str | None:
    """Retrieve a cached diff from S3.

    Returns:
        Diff content string, or None if not cached.
    """
    try:
        client = get_s3_client()
        response = client.get_object(Bucket=_bucket_name(), Key=_diff_key(repo, pr, sha))
        return response["Body"].read().decode("utf-8")
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return None
        raise


def put_diff(repo: str, pr: int, sha: str, diff_content: str) -> None:
    """Store a diff in S3 cache."""
    client = get_s3_client()
    client.put_object(
        Bucket=_bucket_name(),
        Key=_diff_key(repo, pr, sha),
        Body=diff_content.encode("utf-8"),
        ContentType="text/plain",
    )


def get_cached_analysis(repo: str, pr: int, sha: str) -> list[Finding] | None:
    """Retrieve cached analysis findings from S3.

    Returns:
        List of Finding objects, or None if not cached.
    """
    try:
        client = get_s3_client()
        response = client.get_object(Bucket=_bucket_name(), Key=_analysis_key(repo, pr, sha))
        data = json.loads(response["Body"].read().decode("utf-8"))
        return [Finding(**f) for f in data]
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return None
        raise


def put_analysis(repo: str, pr: int, sha: str, findings: list[Finding]) -> None:
    """Store analysis findings in S3 cache."""
    client = get_s3_client()
    client.put_object(
        Bucket=_bucket_name(),
        Key=_analysis_key(repo, pr, sha),
        Body=json.dumps([f.model_dump() for f in findings]).encode("utf-8"),
        ContentType="application/json",
    )
