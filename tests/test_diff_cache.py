"""Tests for the S3 diff caching layer."""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

from code_review_agent.diff_cache import (
    get_cached_analysis,
    get_cached_diff,
    put_analysis,
    put_diff,
)
from code_review_agent.models import Finding

_SHA_A = "abc123def456789012345678901234567890abcd"
_SHA_MISS = "1111111111111111111111111111111111111111"


def test_put_and_get_diff(s3_bucket, sample_diff: str) -> None:
    """Test storing and retrieving a diff from cache."""
    put_diff("owner/repo", 42, _SHA_A, sample_diff)
    result = get_cached_diff("owner/repo", 42, _SHA_A)
    assert result == sample_diff


def test_get_diff_cache_miss(s3_bucket) -> None:
    """Test that cache miss returns None."""
    result = get_cached_diff("owner/repo", 99, _SHA_MISS)
    assert result is None


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
    put_analysis("owner/repo", 42, _SHA_A, findings)
    result = get_cached_analysis("owner/repo", 42, _SHA_A)
    assert result is not None
    assert len(result) == 1
    assert result[0].file == "src/main.py"
    assert result[0].severity == "warning"


def test_get_analysis_cache_miss(s3_bucket) -> None:
    """Test that analysis cache miss returns None."""
    result = get_cached_analysis("owner/repo", 99, _SHA_MISS)
    assert result is None


def test_get_cached_analysis_returns_none_on_stale_json(s3_bucket) -> None:
    """Bad JSON content should be treated as a cache miss, not an exception."""
    s3_bucket.put_object(
        Bucket="test-diff-cache-bucket",
        Key=f"analyses/owner/repo/1/{_SHA_A}.json",
        Body=b'{"broken',
    )
    result = get_cached_analysis("owner/repo", 1, _SHA_A)
    assert result is None


def test_get_cached_analysis_returns_none_on_bad_schema(s3_bucket) -> None:
    """Valid JSON not matching Finding schema should be treated as cache miss."""
    s3_bucket.put_object(
        Bucket="test-diff-cache-bucket",
        Key=f"analyses/owner/repo/1/{_SHA_A}.json",
        Body=b'[{"weird_field": 1}]',
    )
    result = get_cached_analysis("owner/repo", 1, _SHA_A)
    assert result is None


def test_get_cached_analysis_rejects_repo_traversal(s3_bucket) -> None:
    """Repo with traversal-style leading dots should raise ValueError."""
    with pytest.raises(ValueError, match="Invalid repo format"):
        get_cached_analysis("../evil", 1, _SHA_A)


def test_get_cached_analysis_rejects_bad_sha(s3_bucket) -> None:
    """SHA not matching 40-hex format should raise ValueError."""
    with pytest.raises(ValueError, match="Invalid SHA format"):
        get_cached_analysis("owner/repo", 1, "not-a-sha")


def test_get_cached_analysis_rejects_nonpositive_pr(s3_bucket) -> None:
    """PR <= 0 should raise ValueError."""
    with pytest.raises(ValueError, match="Invalid PR number"):
        get_cached_analysis("owner/repo", 0, _SHA_A)
    with pytest.raises(ValueError, match="Invalid PR number"):
        get_cached_analysis("owner/repo", -1, _SHA_A)


# ---------------------------------------------------------------------------
# Exception-path coverage (lines 32, 89-91, 108-109, 133-135, 152-153)
# ---------------------------------------------------------------------------


def test_bucket_name_raises_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Line 32: RuntimeError when DIFF_CACHE_BUCKET is not configured."""
    monkeypatch.delenv("DIFF_CACHE_BUCKET", raising=False)
    from code_review_agent.diff_cache import _bucket_name

    with pytest.raises(RuntimeError, match="DIFF_CACHE_BUCKET"):
        _bucket_name()


def test_get_cached_diff_returns_none_on_client_error(
    s3_bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lines 89-91: non-NoSuchKey ClientError returns None, not raises."""
    from unittest.mock import MagicMock

    import code_review_agent.diff_cache as dc

    mock_client = MagicMock()
    mock_client.exceptions.NoSuchKey = s3_bucket.exceptions.NoSuchKey
    mock_client.get_object.side_effect = ClientError(
        {"Error": {"Code": "InternalError", "Message": "transient"}},
        "GetObject",
    )
    monkeypatch.setattr(dc, "_client", mock_client)

    result = get_cached_diff("owner/repo", 1, _SHA_A)
    assert result is None


def test_put_diff_swallows_client_error(s3_bucket, monkeypatch: pytest.MonkeyPatch) -> None:
    """Lines 108-109: ClientError on put is logged and swallowed."""
    from unittest.mock import MagicMock

    import code_review_agent.diff_cache as dc

    mock_client = MagicMock()
    mock_client.put_object.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "no perms"}},
        "PutObject",
    )
    monkeypatch.setattr(dc, "_client", mock_client)

    # Must not raise
    put_diff("owner/repo", 1, _SHA_A, "diff content")
    mock_client.put_object.assert_called_once()


def test_get_cached_analysis_returns_none_on_client_error(
    s3_bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lines 133-135: non-NoSuchKey ClientError returns None."""
    from unittest.mock import MagicMock

    import code_review_agent.diff_cache as dc

    mock_client = MagicMock()
    mock_client.exceptions.NoSuchKey = s3_bucket.exceptions.NoSuchKey
    mock_client.get_object.side_effect = ClientError(
        {"Error": {"Code": "SlowDown", "Message": "throttled"}},
        "GetObject",
    )
    monkeypatch.setattr(dc, "_client", mock_client)

    result = get_cached_analysis("owner/repo", 1, _SHA_A)
    assert result is None


def test_put_analysis_swallows_client_error(s3_bucket, monkeypatch: pytest.MonkeyPatch) -> None:
    """Lines 152-153: ClientError on put is logged and swallowed."""
    from unittest.mock import MagicMock

    import code_review_agent.diff_cache as dc

    mock_client = MagicMock()
    mock_client.put_object.side_effect = ClientError(
        {"Error": {"Code": "ServiceUnavailable", "Message": "retry later"}},
        "PutObject",
    )
    monkeypatch.setattr(dc, "_client", mock_client)

    findings = [
        Finding(
            file="src/x.py",
            line=1,
            severity="info",
            message="test",
            suggestion=None,
        )
    ]
    # Must not raise
    put_analysis("owner/repo", 1, _SHA_A, findings)
    mock_client.put_object.assert_called_once()
