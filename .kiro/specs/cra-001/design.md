# CRA-001: Automated PR Review Pipeline — Design

## Architecture Overview

```
GitHub webhook
    │
    ▼
API Gateway HTTP
    │
    ▼
Lambda (handler.py)
    │
    ├──▶ Webhook Validator ──▶ filter by event/action ──▶ [200 no-op if filtered]
    │
    ├──▶ Secrets Manager (cold-start: fetch GH token + webhook secret)
    │
    ├──▶ Diff Cache (S3) ──▶ check cached analysis ──▶ [reuse if hit]
    │
    ├──▶ GitHub API ──▶ fetch PR diff
    │
    ├──▶ Diff Eligibility Filter ──▶ apply denylist, count excluded ──▶ EligibleDiff
    │
    ├──▶ Reviewability Gate ──▶ >50 eligible? ──▶ [200 no-op + "too large" comment]
    │                       ──▶ 0 files? ──▶ [200 no-op + "no changes" comment]
    │
    ├──▶ Bedrock (Haiku) ──▶ analyze eligible diff ──▶ raw findings
    │
    ├──▶ Finding Validator ──▶ parse hunks, validate lines ──▶ valid findings
    │                      ──▶ out-of-hunk? ──▶ [200 + structured log, no review]
    │
    ├──▶ Deduplication Check ──▶ head-SHA marker exists? ──▶ [200 no-op, skip post]
    │
    ├──▶ Review Publisher ──▶ cap at 20 inline, overflow to body ──▶ GitHub review
    │                     ──▶ rate-limit 403? ──▶ [neutral comment + ReviewFailed]
    │
    └──▶ Observability ──▶ structured log + CloudWatch metrics
```

## Execution Model

The Lambda performs the complete pipeline synchronously within a single invocation. It returns HTTP 200 only after:
- Successful review publication, OR
- A deterministic no-op outcome (filtered event, PR too large, empty PR, dedup hit, out-of-hunk findings), OR
- Successful neutral comment posting (rate limit, too large, empty).

Transient failures (GitHub API errors, Bedrock timeouts) return non-2xx status codes so GitHub can retry the webhook.

## Component Design

### 1. Lambda Handler (`handler.py`)
**Entry:** `lambda_handler(event, context)`

**Orchestration flow:**
1. Validate webhook signature using HMAC-SHA256 with secret from Secrets Manager. Invalid → 401.
2. Filter by `X-GitHub-Event` header. Not `pull_request` → 200 no-op.
3. Filter by action. Not `opened` or `synchronize` → 200 no-op.
4. Parse payload: extract repo, PR number, head SHA, PR URL.
5. Check S3 analysis cache for `{repo}/{pr}/{head_sha}`. If hit → reuse findings, skip to step 10.
6. Fetch complete diff from GitHub API via MCP or direct call. Retry once on transient failure. Hard failure → 502.
7. Cache raw diff in S3 `diffs/{repo}/{pr}/{head_sha}.diff`.
8. Apply eligibility filter (denylist + binary detection). If >50 eligible files → post neutral "too large" comment, return 200 no-op. If 0 files → post neutral "no changes" comment, return 200 no-op.
9. Send eligible diff to Bedrock. Read model ID from `BEDROCK_MODEL_ID` env var (default: `anthropic.claude-3-haiku-20240307`). Reject non-Haiku models before invocation. Bedrock timeout/error → log, 500.
10. Parse Bedrock response into `Finding` objects. Drop findings with `line < 1`.
11. Validate remaining findings against parsed unified diff hunks. File must exist in eligible diff; line must fall within a `+` hunk on the right side. If any valid finding remains out-of-hunk → log structured failure, return 200 (data quality issue, not transient).
12. Cache validated findings in S3 `analyses/{repo}/{pr}/{head_sha}.json`.
13. Check for existing bot-authored reviews with the same head-SHA marker. If found → return 200 no-op (dedup).
14. Build GitHub review: cap at 20 inline comments prioritized by severity (error > warning > info) and diff order. Overflow findings go into fenced code block in review body. Include severity counts, excluded-file count, truncation note (if applicable), and dedup marker `<!-- cra-dedup: {head_sha} -->`.
15. Post review to GitHub. On HTTP 403 with `X-RateLimit-Remaining: 0` → skip review, post neutral issue comment, emit `ReviewFailed`, return 200. On other GitHub failure → retry once, then log and return 502.
16. Emit structured log and CloudWatch metrics. Return 200.

**Environment variables:**
- `DIFF_CACHE_BUCKET` — S3 bucket name (required).
- `SECRETS_ARN` — Secrets Manager ARN for GitHub credentials (required).
- `BEDROCK_MODEL_ID` — Bedrock model identifier (default: `anthropic.claude-3-haiku-20240307`).

### 2. Webhook Validator (`webhook_validator.py`)
**Functions:**
- `validate_signature(payload: bytes, signature: str, secret: str) -> bool` — HMAC-SHA256 validation.
- `filter_event(headers: dict) -> tuple[bool, str]` — Returns `(should_process, reason)`. Only `X-GitHub-Event: pull_request` passes.
- `filter_action(action: str) -> bool` — Only `opened` and `synchronize` pass.

### 3. Diff Cache (`diff_cache.py`)
**Functions:**
- `get_cached_diff(repo, pr, sha) -> str | None`
- `put_diff(repo, pr, sha, diff_content) -> None`
- `get_cached_analysis(repo, pr, sha) -> list[Finding] | None`
- `put_analysis(repo, pr, sha, findings) -> None`

**S3 key pattern:** `diffs/{repo}/{pr}/{sha}.diff`, `analyses/{repo}/{pr}/{sha}.json`

**Behavior:** Bucket name resolved at call time from `DIFF_CACHE_BUCKET` env var; raises `RuntimeError` if unset.

### 4. Diff Eligibility Filter (`diff_filter.py`)
**Data class:**
```python
@dataclass
class EligibleDiff:
    content: str  # filtered diff content
    excluded_files: list[str]
    excluded_count: int
    total_files: int
    is_empty: bool
    too_large: bool  # >50 eligible files
```

**Function:**
- `filter_diff(raw_diff: str) -> EligibleDiff`

**Denylist:** `*.lock`, `*.min.*`, `package-lock.json`, `yarn.lock`, `poetry.lock`, `Cargo.lock`. Binary files identified by MIME type or GitHub diff metadata (`Binary files differ`) are also excluded.

**Logic:**
1. Parse unified diff into file entries.
2. For each file, check against denylist and binary detection.
3. Retain eligible files in output diff.
4. Set `too_large = True` if eligible count > 50.
5. Set `is_empty = True` if eligible count == 0.

### 5. Diff Parser and Hunk Validator (`diff_parser.py`)
**Data class:**
```python
@dataclass
class HunkRange:
    start_line: int  # absolute line in new file
    line_count: int
```

**Functions:**
- `parse_unified_diff(diff: str) -> dict[str, list[HunkRange]]` — Returns map of `{file_path: [HunkRange, ...]}` for all `+` hunks.
- `validate_finding(finding: Finding, hunk_map: dict[str, list[HunkRange]]) -> bool` — Returns `True` if finding's file exists and line falls within a `+` hunk.

**Hunk parsing:** Extract lines starting with `@@` to determine right-side line ranges. Example: `@@ -10,5 +12,7 @@` means new file lines 12–18 are valid.

### 6. Reviewer (`reviewer.py`)
**Function:**
- `analyze_diff(diff: str, model_id: str = "anthropic.claude-3-haiku-20240307") -> list[Finding]`

**Logic:**
1. Reject if `model_id` does not match `anthropic.claude-3-haiku*`. Raise `ValueError` with message stating only Haiku is permitted.
2. Build structured prompt with system message defining the reviewer persona and JSON output schema.
3. Invoke Bedrock `invoke_model` with `max_tokens=4096`.
4. Parse response `content[0].text` as JSON.
5. Validate each finding with Pydantic `Finding` model.
6. Return list of findings.

**Prompt structure:**
- System: Define reviewer persona, output schema (JSON array of findings), and severity definitions.
- User: `"Review this pull request diff and provide findings:\n\n```diff\n{diff}\n```"`

### 7. Review Publisher (`review_publisher.py`)
**Function:**
- `publish_review(repo: str, pr: int, head_sha: str, findings: list[Finding], summary_data: dict) -> PublishResult`

**Data class:**
```python
@dataclass
class PublishResult:
    success: bool
    review_id: str | None
    skipped_reason: str | None  # "rate_limit", "dedup", etc.
```

**Logic:**
1. Check for existing bot reviews with dedup marker `<!-- cra-dedup: {head_sha} -->` in body. If found → return `PublishResult(success=True, review_id=None, skipped_reason="dedup")`.
2. Sort findings by severity priority: error > warning > info, then by file path and line number.
3. Select first 20 findings for inline comments.
4. Remaining findings → render as fenced code block in review body.
5. Build review body with:
   - Severity counts table.
   - Excluded-file count.
   - Truncation note (if applicable).
   - Overflow findings block (if applicable).
   - Dedup marker: `<!-- cra-dedup: {head_sha} -->`.
6. Call GitHub API `POST /repos/{owner}/{repo}/pulls/{pr}/reviews` with `event: "COMMENT"`, inline comments array, and body.
7. Handle HTTP 403 with `X-RateLimit-Remaining: 0` → return `PublishResult(success=False, skipped_reason="rate_limit")`. Handler posts neutral issue comment.
8. On other GitHub failure → retry once. Still fails → return `PublishResult(success=False, skipped_reason="github_error")`.
9. Return `PublishResult(success=True, review_id=response["id"])`.

### 8. Observability (`observability.py`)
**Functions:**
- `emit_structured_log(event: str, pr_url: str, repo: str, action: str, head_sha: str, review_id: str | None, status: str, **kwargs)` — Emits JSON log to CloudWatch.
- `emit_metric(metric_name: str, value: float, dimensions: dict)` — Emits CloudWatch custom metric.

**Log schema:**
```json
{
  "event": "pr_review_completed",
  "pr_url": "https://github.com/owner/repo/pull/42",
  "repo": "owner/repo",
  "action": "opened",
  "head_sha": "abc123",
  "review_id": "123456789",
  "status": "success",
  "severity_counts": {"error": 2, "warning": 5, "info": 3},
  "excluded_files": 3,
  "timestamp": "2026-07-24T03:00:00Z"
}
```

**Metrics:**
- `ReviewCompleted` with dimensions `{repo, severity_count}` — emitted on successful review post.
- `ReviewFailed` with dimensions `{repo, reason}` — emitted on rate-limit, out-of-hunk, or GitHub error.

### 9. Models (`models.py`)
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

class RepoInfo(BaseModel):
    full_name: str
    owner: str
    name: str

class PRInfo(BaseModel):
    number: int
    head_sha: str
    title: str
    diff_url: str
```

### 10. MCP Server (`mcp_server/server.py`)
**Transport:** stdio

**Tools:**
- `read_pr_diff(owner: str, repo: str, pr_number: int) -> str` — Fetches unified diff from GitHub API. Requires GitHub token from environment or config.
- `post_review_comment(owner: str, repo: str, pr_number: int, commit_id: str, review_body: str, comments: list[dict]) -> str` — Submits GitHub PR review. Returns review ID.

**Note:** MCP server is optional in v1. Handler can call GitHub API directly with the token from Secrets Manager.

## Security Design

- Webhook secret stored in Secrets Manager; fetched once per Lambda cold start, cached in memory for the lifetime of the execution environment.
- GitHub App token stored in Secrets Manager; fetched at cold start.
- No secrets in environment variables or code.
- Input validation via Pydantic on all external payloads.
- HMAC-SHA256 webhook signature validation before any processing.

## Error Handling

| Scenario | Action | HTTP Status | GitHub Retry? |
|----------|--------|-------------|---------------|
| Invalid or missing signature | Log, do not process | 401 | No |
| Filtered event (not `pull_request`) | Log, return immediately | 200 | No |
| Filtered action (not `opened`/`synchronize`) | Log, return immediately | 200 | No |
| PR too large (>50 eligible files) | Post neutral "too large" comment | 200 | No |
| Empty PR (0 files) | Post neutral "no changes" comment | 200 | No |
| GitHub API failure (fetch diff) | Retry once, then log | 502 | Yes |
| Bedrock timeout/error | Log, do not post review | 500 | Yes |
| Out-of-hunk findings after validation | Log structured failure, do not post | 200 | No (data quality) |
| Dedup hit (marker exists) | Log, skip posting | 200 | No |
| GitHub rate-limit exhausted (403) | Post neutral issue comment, emit `ReviewFailed` | 200 | No |
| GitHub review post failure (non-rate-limit) | Retry once, log, emit `ReviewFailed` | 502 | Yes |
| Cache miss | Not an error; proceed with fetch + analyze | N/A | N/A |

**Rationale for 200 on out-of-hunk findings:** The invalid finding is a data quality issue from Bedrock (hallucinated line numbers), not a transient infrastructure failure. The pipeline executed deterministically. GitHub should not retry. The system logs the failure for observability and operator intervention.

## Cost Controls

- **Cache-first architecture:** Check S3 analysis cache before invoking Bedrock. Duplicate `{repo}/{pr}/{head_sha}` analyses are never re-invoked.
- **Model enforcement:** Read `BEDROCK_MODEL_ID` from environment (default: `anthropic.claude-3-haiku-20240307`). Reject non-Haiku models at runtime before invocation. This allows operator override for approved model updates while maintaining cost governance by default.
- **Eligibility filtering:** Denylist excludes lock files, minified files, and binaries from the Bedrock prompt, reducing token cost without losing the complete diff cache.
- **Reviewability gate:** PRs with >50 eligible files skip Bedrock entirely, posting a neutral comment instead.
- **S3 lifecycle policy:** Automatic 7-day expiry on all cached objects.
- **CloudWatch alarm:** Alert on >100 Bedrock invocations per day during development.

## Deferred to Future Versions

- **Replay protection:** Webhook authentication uses HMAC-SHA256 without timestamp validation in v1. Replay attacks are accepted as low-risk; defense requires tracking nonce/timestamp state.
- **Content-hash deduplication:** Cache keys remain SHA-based. Identical diff content in different PRs is cached separately in v1.
- **Multi-line findings:** Schema uses single `line: int`. Support for `start_line`/`end_line` is deferred.
- **Per-repository cost tracing:** CloudWatch metrics include repo dimension but do not enforce per-repo budgets in v1.
