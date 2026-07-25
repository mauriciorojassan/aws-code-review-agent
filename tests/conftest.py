"""Shared pytest fixtures for Code Review Agent tests."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws


@pytest.fixture(autouse=True)
def _env_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set environment variables for all tests."""
    monkeypatch.setenv("DIFF_CACHE_BUCKET", "test-diff-cache-bucket")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")


@pytest.fixture
def s3_bucket():
    """Create a mocked S3 bucket for testing."""
    from code_review_agent import diff_cache

    diff_cache._client = None  # reset lazy singleton before moto context
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-diff-cache-bucket")
        yield client
    # Reset again on teardown so a stale (post-moto) client cannot leak
    # into a subsequent test that skips this fixture.
    diff_cache._client = None


@pytest.fixture
def sample_diff() -> str:
    """Return a sample unified diff for testing."""
    return """diff --git a/src/main.py b/src/main.py
index 1234567..abcdefg 100644
--- a/src/main.py
+++ b/src/main.py
@@ -1,5 +1,6 @@
 import os
+import sys
 
 def main():
-    print("hello")
+    print("hello world")
     return 0
"""  # noqa: W293


@pytest.fixture
def sample_findings() -> list[dict]:
    """Return sample findings for testing."""
    return [
        {
            "file": "src/main.py",
            "line": 2,
            "severity": "info",
            "message": "Unused import: sys is imported but never used",
            "suggestion": "Remove the unused import",
        },
    ]
