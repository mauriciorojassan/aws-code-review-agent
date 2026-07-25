# CRA-001: Automated PR Review Pipeline — Tasks

## Wave 1: Foundation (completed)

- [x] **T1.1** Create `src/code_review_agent/models.py` — Pydantic models for Finding, WebhookPayload, ReviewResult, RepoInfo, PRInfo.
- [x] **T1.2** Create `src/code_review_agent/diff_cache.py` — S3 get/put for diffs and analyses with call-time env resolution.
- [x] **T1.3** Create `tests/test_diff_cache.py` — Unit tests with moto S3 mock (4 tests, all passing).
- [x] **T1.4** Create `template.yaml` — SAM template with Lambda, API Gateway HTTP, S3 bucket, Secrets Manager, IAM roles, CloudWatch alarm, S3 lifecycle (scaffolded in foundation commit).

**Status:** Wave 1 gate passed at 97% coverage, ruff/black clean, sam validate clean (commit 4be4906).

## Wave 2: Validation and Filtering (completed)

- [x] **T2.1** Create `src/code_review_agent/webhook_validator.py` — HMAC-SHA256 signature validation, event header filter (`X-GitHub-Event: pull_request`), action filter (`opened`, `synchronize`).
- [x] **T2.2** Create `tests/test_webhook_validator.py` — Unit tests for signature validation (valid, invalid, missing), event filtering, action filtering.
- [x] **T2.3** Create `src/code_review_agent/diff_filter.py` — Eligibility filter with denylist (`*.lock`, `*.min.*`, package-lock, yarn.lock, poetry.lock, Cargo.lock, binary detection). Returns `EligibleDiff` dataclass with content, excluded list, counts, `too_large` flag (>50), `is_empty` flag (0 files).
- [x] **T2.4** Create `tests/test_diff_filter.py` — Unit tests for denylist matching, binary detection, eligible count logic, edge cases (all excluded, all eligible, exactly 50).
- [x] **T2.5** Create `src/code_review_agent/diff_parser.py` — Unified diff hunk parser. `parse_unified_diff(diff: str) -> dict[str, list[HunkRange]]` extracts right-side line ranges from `@@` markers. `validate_finding(finding, hunk_map) -> bool` checks if finding's file and line fall within valid `+` hunks.
- [x] **T2.6** Create `tests/test_diff_parser.py` — Unit tests for hunk parsing (single file, multi-file, edge cases), finding validation (valid, out-of-range, non-existent file, line < 1).

**Status:** Wave 2 gate passed at 100% coverage on `webhook_validator.py`, `diff_filter.py`, and `diff_parser.py` (primary commits: f30a450 webhook validator, aa7f6f2 diff filter, 4c61c18 diff parser; follow-up hardening: d2a1062 signature timing-leak fix, e7f1909 diff-filter regex fix).

## Wave 3: Analysis and Review Logic (completed)

- [x] **T3.1** Update `src/code_review_agent/reviewer.py` — Add `BEDROCK_MODEL_ID` env var support with default `anthropic.claude-3-haiku-20240307`. Add model validation: reject non-Haiku models with `ValueError` before invocation. Update `analyze_diff(diff: str, model_id: str = ...)` signature.
- [x] **T3.2** Create `tests/test_reviewer.py` — Unit tests mocking Bedrock responses with `moto` or direct `botocore.stub.Stubber`. Test: successful parse, non-Haiku rejection, Bedrock error handling, malformed JSON response, line < 1 filtering.
- [x] **T3.3** Create `src/code_review_agent/review_publisher.py` — `publish_review(repo, pr, head_sha, findings, summary_data) -> PublishResult`. Logic: dedup check (query existing reviews for marker), sort findings by severity priority, cap at 20 inline comments, overflow to body, build review body with counts/marker/excluded-file-count, handle GitHub rate-limit 403, retry once on other failures.
- [x] **T3.4** Create `tests/test_review_publisher.py` — Unit tests with `httpx` mocked GitHub API. Test: dedup hit, <20 findings, >20 findings (overflow), rate-limit 403, GitHub error + retry, successful post.
- [x] **T3.5** Create `src/code_review_agent/observability.py` — `emit_structured_log(...)` for JSON CloudWatch logs, `emit_metric(...)` for CloudWatch custom metrics (`ReviewCompleted`, `ReviewFailed`).
- [x] **T3.6** Create `tests/test_observability.py` — Unit tests with moto CloudWatch Logs and CloudWatch mocks. Verify log schema and metric emission.

**Status:** Wave 3 gate passed at 100% coverage on `reviewer.py`, `review_publisher.py`, and `observability.py` after judgment-day hardening (primary commits: c4af900 reviewer, d6a7d87 review publisher, 3796d4e observability; hardening: 14df58b judgment-day fixes covering BotoCoreError swallow, GitHub 429/Retry-After rate-limit widening, Bedrock timeout config, `Finding.line = Field(gt=0)`).

## Wave 4: Handler Orchestration and Integration (completed)

- [x] **T4.1** Implement `src/code_review_agent/handler.py` — Full synchronous orchestration: validate signature → filter event/action → fetch diff (with retry) → cache diff → filter eligibility → reviewability gate (>50 or 0 files) → check analysis cache → analyze with Bedrock → validate findings against hunks → cache analysis → dedup check → publish review (cap/overflow/rate-limit) → emit logs/metrics. Return 200 on success/no-op, 401 on invalid signature, 5xx on transient failures.
- [x] **T4.2** Create `tests/test_handler.py` — Integration tests for full handler flow. Scenarios: valid webhook → analysis → review post; filtered event (200 no-op); invalid signature (401); PR too large (200 + neutral comment); empty PR (200 + neutral comment); out-of-hunk findings (200 + log); dedup hit (200); rate-limit 403 (200 + neutral issue comment); GitHub error (502); Bedrock error (500); cache hit (skip Bedrock).
- [x] **T4.3** Wire handler to Secrets Manager — Fetch webhook secret and GitHub token at cold start, cache in memory.
- [x] **T4.4** End-to-end smoke test — Use `sam local invoke` with a fixture event JSON simulating a `pull_request.opened` webhook. Verify logs, metrics stub, and mock GitHub review creation.

**Status:** Wave 4 gate passed. Handler wires 16-step synchronous pipeline (commit 4f72e24 for T4.1+T4.2 + `github_client.py` + `review_publisher.post_issue_comment` enabler in 1acd994). Secrets Manager fetch with env-first fallback (commit f856db0 for T4.3). Smoke-test fixtures + runbook landed in the follow-up commit (see `docs/smoke-test.md` for step-by-step verification). Three verification steps confirmed passing on this dev host: direct-Python handler invocation (< 2s, correct HTTP 200 filter response), `sam validate --lint`, and `sam build`. `sam local invoke` is exercised in the runbook but blocked in this specific env by a SAM CLI ↔ Docker API version mismatch (documented in the runbook's known-caveat section); the same code path runs correctly outside Docker via Step 1.

## Wave 5: MCP Server (optional, depends on Wave 4)

- [ ] **T5.1** Implement `mcp_server/server.py` — MCP stdio server with `read_pr_diff` and `post_review_comment` tools.
- [ ] **T5.2** Create `tests/test_mcp_server.py` — MCP tool unit tests with mocked GitHub API.
- [ ] **T5.3** (Optional) Wire handler to use MCP tools instead of direct GitHub API calls. Evaluate trade-off: MCP adds abstraction but also latency and complexity in v1.

## Wave 6: Deployment and CI (completed)

- [x] **T6.1** Secrets Manager resource — Already in `template.yaml` (scaffolded in foundation commit).
- [x] **T6.2** CloudWatch alarm for Bedrock invocation count — Already in `template.yaml` (scaffolded in foundation commit).
- [x] **T6.3** S3 lifecycle rule (7-day expiry) — Already in `template.yaml` (scaffolded in foundation commit).
- [x] **T6.4** `sam build && sam deploy --guided` runbook — Documented in `docs/deployment.md` with parallel PAT and GitHub App auth walkthroughs, prerequisites, IAM permissions summary, guided-deploy prompt table, webhook configuration, rollback/teardown, and a "Future — auto-refresh in Lambda" section for the deferred JWT exchange.
- [x] **T6.5** Create `.github/workflows/ci.yml` — Single-job workflow on `push` and `pull_request` to `main`. Runs `ruff check`, `black --check`, `pytest --cov=code_review_agent --cov-fail-under=98`, and `sam validate --lint` on Python 3.12 (matches Lambda runtime). Concurrency group cancels superseded runs.
- [x] **T6.6** Smoke test with `sam local invoke` — `docs/smoke-test.md` covers direct-Python invocation (Step 1, always works), `sam validate --lint` (Step 2), `sam build` (Step 3), and `sam local invoke` (Step 4, Docker-dependent) with a documented SAM CLI ↔ Docker API-drift caveat. README's Quick Start now cross-links both `docs/smoke-test.md` and `docs/deployment.md`.

**Status:** Wave 6 gate passed. CI workflow landed in commit b15345c (T6.5). Deployment runbook landed in commit 8b678f7 (T6.4). README cross-links + CI badge landed in commit 96ed23a (T6.6). Also resolved during this wave: the F8 markdown-escaping item deferred from the Wave 3 judgment day — a targeted `_escape_markdown` helper (strip triple-backticks + leading pipes) was added at render time in `_finding_to_comment` and `_build_review_body` under commit adc47f8, with 16 new tests covering unit behavior and the "overflow fence never breaks" invariant. Final gate: 301 tests pass, src total coverage 98.56%, all touched modules at 100%; ruff + black clean; `sam validate --lint` clean.

## Definition of Done

- All tests pass (`pytest --cov` ≥ 80% on `src/code_review_agent/`).
- `ruff check src/ tests/ mcp_server/` and `black --check src/ tests/ mcp_server/` pass with zero findings.
- `sam validate --lint` passes on `template.yaml`.
- Cost governance constraints verified:
  - Only `anthropic.claude-3-haiku` model permitted (test with non-Haiku ID → `ValueError`).
  - Analysis cache hit avoids Bedrock re-invocation (verified in `test_handler.py`).
  - CloudWatch alarm configured for >100 daily invocations (present in `template.yaml`).
- Structured logging emits all required fields (`pr_url`, `repo`, `action`, `head_sha`, `review_id`, `status`).
- CloudWatch metrics `ReviewCompleted` and `ReviewFailed` emit with correct dimensions.
- `README.md` includes deployment runbook with webhook URL setup.
- `.github/workflows/ci.yml` exists and runs the full gate on every push.

## Requirements Traceability Matrix

The following table maps each Acceptance Criterion in `requirements.md` to the task(s) that implement and verify it:

| Requirement | AC | Implemented By | Verified By |
|-------------|-----|----------------|-------------|
| US-1 | Exposes HTTPS endpoint | T1.4 (template.yaml), T4.1 (handler) | T4.2 (integration test) |
| US-1 | Validates signature (HMAC-SHA256) | T2.1 (webhook_validator), T4.1 | T2.2, T4.2 (401 scenario) |
| US-1 | Filters by `X-GitHub-Event: pull_request` | T2.1, T4.1 | T2.2, T4.2 (filtered event → 200) |
| US-1 | Filters by action (`opened`, `synchronize`) | T2.1, T4.1 | T2.2, T4.2 (filtered action → 200) |
| US-1 | Synchronous full pipeline, returns 200 after completion/no-op | T4.1 | T4.2 (all scenarios) |
| US-2 | Fetches complete diff from GitHub | T4.1 (fetch step) | T4.2 |
| US-2 | Returns unified diff format | T4.1 | T4.2 |
| US-2 | Caches complete diff in S3 before analysis | T1.2 (diff_cache), T4.1 | T1.3, T4.2 |
| US-2 | Excludes denylisted files from Bedrock prompt | T2.3 (diff_filter), T4.1 | T2.4, T4.2 |
| US-2 | Records excluded-file count | T2.3, T4.1 | T2.4, T3.4 (review body) |
| US-2 | PR >50 eligible files → no Bedrock, neutral comment | T2.3 (too_large flag), T4.1 | T2.4, T4.2 (too large scenario) |
| US-2 | PR 0 files → neutral "no changes" comment | T2.3 (is_empty flag), T4.1 | T2.4, T4.2 (empty scenario) |
| US-3 | Sends eligible diff to Bedrock Haiku | T3.1 (reviewer), T4.1 | T3.2, T4.2 |
| US-3 | Reads `BEDROCK_MODEL_ID`, defaults to Haiku, rejects non-Haiku | T3.1 | T3.2 (non-Haiku rejection test) |
| US-3 | Truncates at 100 hunks if oversized | T4.1 (orchestration) | T4.2 (edge case) |
| US-3 | Response schema with file, line, severity, message, suggestion | T3.1 (reviewer parse) | T3.2 |
| US-3 | `line` >= 1; drops findings with line < 1 | T3.1, T2.5 (validator) | T3.2, T2.6 |
| US-3 | Validates findings against right-side + hunks | T2.5 (diff_parser), T4.1 | T2.6, T4.2 (out-of-hunk scenario) |
| US-3 | Out-of-hunk finding → skip publication, log failure | T4.1 | T4.2 (out-of-hunk → 200 + log) |
| US-3 | Severity definitions (error/warning/info) | T1.1 (models), T3.1 | Documented in design.md |
| US-3 | Reuses cached analysis if identical SHA | T1.2, T4.1 | T1.3, T4.2 (cache hit scenario) |
| US-4 | Creates one GitHub PR review, not per-finding | T3.3 (review_publisher), T4.1 | T3.4, T4.2 |
| US-4 | Caps at 20 inline comments, overflow to body by severity priority | T3.3 | T3.4 (>20 findings test) |
| US-4 | Review body includes counts, excluded files, truncation note | T3.3 | T3.4 |
| US-4 | Zero valid findings → summary review, no inline | T3.3, T4.1 | T3.4, T4.2 |
| US-4 | Atomic publication: failure → no partial review | T3.3, T4.1 | T3.4, T4.2 |
| US-4 | Dedup marker with head-SHA, checks before posting | T3.3 (dedup check) | T3.4 (dedup hit test) |
| US-4 | GitHub 403 rate-limit → neutral issue comment, `ReviewFailed` | T3.3, T4.1 | T3.4, T4.2 (rate-limit scenario) |
| US-4 | GitHub App token supports public + private repos | T4.3 (Secrets Manager fetch) | Deployment verification |
| US-5 | Only Haiku model permitted; Sonnet/Opus rejected | T3.1 | T3.2 (non-Haiku ValueError) |
| US-5 | Duplicate analyses never re-invoked after cache hit | T1.2, T4.1 | T1.3, T4.2 (cache hit) |
| US-5 | CloudWatch alarm on >100 daily Bedrock invocations | T1.4 (template.yaml), T6.2 | `sam validate`, deployment verification |
| US-5 | S3 objects expire after 7 days | T1.4 (template.yaml), T6.3 | `sam validate`, deployment verification |
| NFR | Synchronous path completes within 30s at p95 | T4.1 (orchestration), Lambda timeout 30s | T4.2 (integration timing) |
| NFR | Cold-start < 5s at p95 | T4.3 (Secrets Manager fetch optimized) | Deployment monitoring |
| NFR | Structured JSON logs with required fields | T3.5 (observability), T4.1 | T3.6, T4.2 |
| NFR | CloudWatch metrics `ReviewCompleted`, `ReviewFailed` | T3.5, T4.1 | T3.6, T4.2 |
| NFR | Zero real AWS calls in tests (moto) | All test tasks | All test executions |
| NFR | ≥80% coverage on core modules | All test tasks | CI gate (T6.5) + pytest report |

**Note:** Some cells reference "deployment verification" or "deployment monitoring" — these are post-deploy observability checks, not automated tests in v1.
