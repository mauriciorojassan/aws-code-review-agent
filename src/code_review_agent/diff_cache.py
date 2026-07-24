"""S3 caching layer for diffs and analysis results."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import boto3
from botocore.exceptions import ClientError
from pydantic import ValidationError

from code_review_agent.models import Finding

logger = logging.getLogger(__name__)

# Repo owner and name must each start with an alphanumeric or underscore.
# Excludes leading dots, which prevents `..` traversal-style inputs while
# remaining permissive of every real GitHub owner/repo name.
_REPO_RE = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9._-]*/[a-zA-Z0-9_][a-zA-Z0-9._-]*$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

_client: Any | None = None


def _bucket_name() -> str:
    """Resolve cache bucket name at call time so tests can monkeypatch env."""
    name = os.environ.get("DIFF_CACHE_BUCKET", "")
    if not name:
        raise RuntimeError("DIFF_CACHE_BUCKET environment variable is not set")
    return name


def get_s3_client() -> Any:
    """Return the module-level S3 client, creating it lazily on first use."""
    global _client
    if _client is None:
        _client = boto3.client("s3")
    return _client


def _validate_repo(repo: str) -> str:
    """Validate and return a repo string in ``owner/name`` format."""
    if not _REPO_RE.match(repo):
        raise ValueError(f"Invalid repo format: {repo!r}")
    return repo


def _validate_sha(sha: str) -> str:
    """Validate and return a 40-hex-char Git SHA."""
    if not _SHA_RE.match(sha):
        raise ValueError(f"Invalid SHA format: {sha!r}")
    return sha


def _validate_pr(pr: int) -> int:
    """Validate that the PR number is positive."""
    if pr <= 0:
        raise ValueError(f"Invalid PR number: {pr}")
    return pr


def _diff_key(repo: str, pr: int, sha: str) -> str:
    return f"diffs/{repo}/{pr}/{sha}.diff"


def _analysis_key(repo: str, pr: int, sha: str) -> str:
    return f"analyses/{repo}/{pr}/{sha}.json"


def get_cached_diff(repo: str, pr: int, sha: str) -> str | None:
    """Retrieve a cached diff from S3.

    Returns the diff content on hit, ``None`` on cache miss or a transient
    S3 error. Raises ``ValueError`` on malformed inputs.
    """
    repo = _validate_repo(repo)
    pr = _validate_pr(pr)
    sha = _validate_sha(sha)
    key = _diff_key(repo, pr, sha)
    try:
        client = get_s3_client()
        response = client.get_object(Bucket=_bucket_name(), Key=key)
        return response["Body"].read().decode("utf-8")
    except client.exceptions.NoSuchKey:
        return None
    except ClientError as e:
        logger.error("S3 ClientError on get_cached_diff for %s: %s", key, e)
        return None


def put_diff(repo: str, pr: int, sha: str, diff_content: str) -> None:
    """Store a diff in S3 cache. Best-effort — errors are logged and swallowed."""
    repo = _validate_repo(repo)
    pr = _validate_pr(pr)
    sha = _validate_sha(sha)
    key = _diff_key(repo, pr, sha)
    try:
        client = get_s3_client()
        client.put_object(
            Bucket=_bucket_name(),
            Key=key,
            Body=diff_content.encode("utf-8"),
            ContentType="text/plain",
        )
    except ClientError as e:
        logger.warning("S3 ClientError on put_diff for %s: %s", key, e)


def get_cached_analysis(repo: str, pr: int, sha: str) -> list[Finding] | None:
    """Retrieve cached findings from S3.

    Returns the list of findings on hit, ``None`` on cache miss, stale/corrupt
    entries, or transient S3 errors. Raises ``ValueError`` on malformed inputs.
    """
    repo = _validate_repo(repo)
    pr = _validate_pr(pr)
    sha = _validate_sha(sha)
    key = _analysis_key(repo, pr, sha)
    try:
        client = get_s3_client()
        obj = client.get_object(Bucket=_bucket_name(), Key=key)
        body = obj["Body"].read().decode("utf-8")
        data = json.loads(body)
        return [Finding(**item) for item in data]
    except client.exceptions.NoSuchKey:
        return None
    except (json.JSONDecodeError, ValidationError) as e:
        logger.warning("Stale cache entry at %s: %s", key, e)
        return None
    except ClientError as e:
        logger.error("S3 ClientError on get_cached_analysis for %s: %s", key, e)
        return None


def put_analysis(repo: str, pr: int, sha: str, findings: list[Finding]) -> None:
    """Store findings in S3 cache. Best-effort — errors are logged and swallowed."""
    repo = _validate_repo(repo)
    pr = _validate_pr(pr)
    sha = _validate_sha(sha)
    key = _analysis_key(repo, pr, sha)
    try:
        client = get_s3_client()
        client.put_object(
            Bucket=_bucket_name(),
            Key=key,
            Body=json.dumps([f.model_dump() for f in findings]).encode("utf-8"),
            ContentType="application/json",
        )
    except ClientError as e:
        logger.warning("S3 ClientError on put_analysis for %s: %s", key, e)
