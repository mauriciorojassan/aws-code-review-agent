"""Tests for the S3 diff caching layer."""

from __future__ import annotations

from moto import mock_aws

from code_review_agent.diff_cache import (
    get_cached_analysis,
    get_cached_diff,
    put_analysis,
    put_diff,
)
from code_review_agent.models import Finding


@mock_aws
def test_put_and_get_diff(s3_bucket, sample_diff: str) -> None:
    """Test storing and retrieving a diff from cache."""
    put_diff("owner/repo", 42, "abc123", sample_diff)
    result = get_cached_diff("owner/repo", 42, "abc123")
    assert result == sample_diff


@mock_aws
def test_get_diff_cache_miss(s3_bucket) -> None:
    """Test that cache miss returns None."""
    result = get_cached_diff("owner/repo", 99, "missing")
    assert result is None


@mock_aws
def test_put_and_get_analysis(s3_bucket) -> None:
    """Test storing and retrieving analysis findings."""
    findings = [
        Finding(
            file="src/main.py",
            line=10,
            severity="warning",
            message="Consider using pathlib",
            suggestion="Replace os.path with pathlib.Path",
        )
    ]
    put_analysis("owner/repo", 42, "abc123", findings)
    result = get_cached_analysis("owner/repo", 42, "abc123")
    assert result is not None
    assert len(result) == 1
    assert result[0].file == "src/main.py"
    assert result[0].severity == "warning"


@mock_aws
def test_get_analysis_cache_miss(s3_bucket) -> None:
    """Test that analysis cache miss returns None."""
    result = get_cached_analysis("owner/repo", 99, "missing")
    assert result is None
