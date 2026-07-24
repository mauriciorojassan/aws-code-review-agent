# CRA-001: Automated PR Review Pipeline — Design

## Architecture Overview

```
GitHub ──webhook──▶ API Gateway HTTP ──▶ Lambda (handler.py)
                                              │
                                    ┌─────────┼─────────┐
                                    ▼         ▼         ▼
                              Secrets Mgr   S3 Cache   Bedrock
                              (GH token)    (diff)     (Haiku)
                                    │                    │
                                    ▼                    ▼
                              GitHub API ◀──── findings ─┘
                              (post review)
```

## Component Design

### 1. Lambda Handler (`handler.py`)
- Entry: `lambda_handler(event, context)`
- Validates webhook signature using HMAC-SHA256.
- Parses event to extract: repo, PR number, head SHA, action.
- Orchestrates: cache check → diff fetch → analysis → publish.
- Returns 200 immediately; heavy work is synchronous within 30s timeout.

### 2. Diff Cache (`diff_cache.py`)
- `get_cached_diff(repo, pr, sha) -> str | None`
- `put_diff(repo, pr, sha, diff_content) -> None`
- `get_cached_analysis(repo, pr, sha) -> list[Finding] | None`
- `put_analysis(repo, pr, sha, findings) -> None`
- S3 key pattern: `diffs/{repo}/{pr}/{sha}.diff`, `analyses/{repo}/{pr}/{sha}.json`

### 3. Reviewer (`reviewer.py`)
- `analyze_diff(diff: str) -> list[Finding]`
- Constructs prompt with system message defining the reviewer persona.
- Uses Bedrock `invoke_model` with JSON structured output.
- Parses response into `Finding` Pydantic models.

### 4. Models (`models.py`)
```python
class Finding(BaseModel):
    file: str
    line: int
    severity: Literal["error", "warning", "info"]
    message: str
    suggestion: str | None = None

class WebhookPayload(BaseModel):
    action: str
    number: int
    repository: RepoInfo
    pull_request: PRInfo

class ReviewResult(BaseModel):
    findings: list[Finding]
    cached: bool = False
```

### 5. MCP Server (`mcp_server/server.py`)
- Transport: stdio
- Tools:
  - `read_pr_diff(owner, repo, pr_number) -> str` — calls GitHub API, returns unified diff.
  - `post_review_comment(owner, repo, pr_number, commit_id, findings) -> bool` — submits review.

## Security Design
- Webhook secret stored in Secrets Manager; fetched once per cold start, cached in memory.
- GitHub App token in Secrets Manager; refreshed if expired.
- No secrets in environment variables or code.
- Input validation via Pydantic on all external data.

## Error Handling
- Invalid signature → 401, no processing.
- GitHub API failure → retry once, then log and return 502.
- Bedrock timeout/error → log, return 500, do not post partial review.
- Cache miss is not an error; proceed with fetch + analyze.

## Cost Controls (by design)
- Cache-first architecture eliminates duplicate Bedrock calls.
- Single model hardcoded; no runtime model selection.
- S3 lifecycle policy handles cleanup automatically.
