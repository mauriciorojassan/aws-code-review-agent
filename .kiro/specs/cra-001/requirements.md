# CRA-001: Automated PR Review Pipeline — Requirements

## Scope and Execution Decisions

- **Execution model:** v1 is fully synchronous. The Lambda performs retrieval, analysis, validation, and publication during one invocation and returns only after the pipeline reaches a successful, deterministic no-op, or failure outcome. There is no queue or second-stage worker in v1.
- **Reviewability limit:** The complete diff is cached, but only eligible files are sent to Bedrock. A PR with more than 50 eligible changed files is rejected as too large for v1.
- **Inline publication policy:** Findings are validated before publication, and GitHub publication is atomic. The system never posts a partial review.
- **Finding location:** `line` is an absolute line number in the post-state (new) file, as required by the GitHub review API. It is not a diff-relative line number.

## User Stories

### US-1: Webhook Reception
**As a** repository maintainer,
**I want** the system to automatically receive GitHub PR events,
**So that** reviews begin without manual intervention.

**Acceptance Criteria:**
- [ ] System exposes an HTTPS endpoint that accepts GitHub webhook POST requests.
- [ ] Validates `X-Hub-Signature-256` with the configured webhook secret using HMAC-SHA256. Invalid or missing signatures return HTTP 401 and do not process the payload.
- [ ] Processes a request only when `X-GitHub-Event: pull_request`. Any other event header is ignored and returns HTTP 200 without analysis or publication.
- [ ] Processes only `pull_request.opened` and `pull_request.synchronize` actions. `reopened`, `ready_for_review`, `edited`, `labeled`, `closed`, and all other actions are ignored and return HTTP 200; ignored events do not delete or mutate cache entries.
- [ ] For an accepted event, the Lambda performs the complete pipeline synchronously and returns HTTP 200 only after review publication or a deterministic no-op outcome. Transient processing failures return non-2xx so GitHub can retry.

### US-2: Diff Retrieval and Scope Control
**As a** the review pipeline,
**I want** to fetch and cache the complete PR diff while excluding unsuitable files,
**So that** analysis has relevant file-change context without uncontrolled token cost.

**Acceptance Criteria:**
- [ ] MCP tool `read_pr_diff` fetches the complete diff from the GitHub API.
- [ ] The tool returns unified diff format with file paths and hunk metadata.
- [ ] The complete retrieved diff is cached in S3 under `diffs/{repo}/{pr}/{head_sha}.diff` before analysis.
- [ ] Files matching `*.lock`, `*.min.*`, `package-lock.json`, `yarn.lock`, `poetry.lock`, and `Cargo.lock` are excluded from the Bedrock prompt. Binary files identified by GitHub diff metadata or MIME type are also excluded. The cache still retains the complete original diff.
- [ ] The pipeline records the excluded file count and includes it in the review summary.
- [ ] After exclusions, a PR with 50 or fewer eligible changed files proceeds to analysis.
- [ ] After exclusions, a PR with more than 50 eligible changed files does not invoke Bedrock; the system attempts one neutral review/comment stating that the PR is too large to review automatically and returns a deterministic no-op result.
- [ ] A PR with zero changed files does not invoke Bedrock and receives one neutral comment stating `No changes to review`.

### US-3: AI Analysis and Finding Validation
**As a** developer receiving a review,
**I want** the analysis to be specific, actionable, and safe to publish,
**So that** I can address issues quickly without guessing or receiving invalid comments.

**Acceptance Criteria:**
- [ ] Sends the eligible diff to Amazon Bedrock using `anthropic.claude-3-haiku-20240307` with a structured review prompt. The model identifier is read from `BEDROCK_MODEL_ID`, defaulting to this Haiku identifier; any configured non-Haiku model is rejected without invoking Bedrock.
- [ ] If the eligible diff exceeds the configured context limit, truncates at hunk boundaries to the first 100 eligible hunks and adds a machine-readable truncation note to the analysis input and summary.
- [ ] Response schema contains a list of findings with `file`, `line`, `severity`, `message`, and optional `suggestion`.
- [ ] `line` is an integer greater than or equal to 1 and represents an absolute line in the new/post-state file. Findings with `line < 1` are dropped before publication.
- [ ] Each remaining finding is validated against the parsed unified diff. Its file must exist in the eligible diff and its line must fall within a valid added (`+`) hunk on the right side. A finding outside a valid right-side hunk is invalid.
- [ ] If any finding remains out-of-hunk after validation, the entire review publication is skipped, the failure is logged, and no partial inline review is submitted.
- [ ] Severity values are `error`, `warning`, or `info` and have these meanings: `error` indicates a correctness or security risk, `warning` indicates a meaningful maintainability or reliability risk, and `info` indicates a non-blocking improvement. Severity is informational only in v1 and does not create a GitHub status check or merge block.
- [ ] If an identical `{repo}/{pr}/{head_sha}` analysis is cached, the system does not invoke Bedrock again and reuses the cached findings.

### US-4: Comment and Review Publishing
**As a** developer,
**I want** findings posted as inline PR comments,
**So that** I see them in context on the relevant lines without duplicate or partial reviews.

**Acceptance Criteria:**
- [ ] MCP tool `post_review_comment` creates one GitHub PR review containing all publishable inline findings; it does not create one review per finding.
- [ ] A single review contains at most 20 inline comments. When more than 20 valid findings exist, inline comments are selected deterministically by severity priority (`error`, then `warning`, then `info`) and diff order; overflow findings are rendered in a fenced summary code block in the review body.
- [ ] The review body includes counts for each severity, the number of excluded files, and any truncation note.
- [ ] If analysis produces zero valid findings, the system posts one summary review stating that no actionable findings were detected and creates no inline comments.
- [ ] Publication is atomic: if any valid inline finding cannot be mapped by GitHub or the GitHub review submission fails, the system does not post a partial review, emits a structured failure record, and returns a non-success outcome.
- [ ] Every generated review includes a deduplication marker containing the head SHA. Before posting, the system checks existing bot-authored reviews for the same PR and head-SHA marker; if one exists, it does not create another review.
- [ ] If GitHub returns HTTP 403 with `X-RateLimit-Remaining: 0`, the system skips review publication, attempts one neutral issue comment explaining that the GitHub API rate limit was exhausted, and records `ReviewFailed`; it does not perform an unbounded retry loop.
- [ ] The configured GitHub App token supports both public and private repositories with the required pull-request read/write permissions.

### US-5: Cost Governance
**As a** project owner,
**I want** automated cost controls,
**So that** the system avoids unnecessary model calls and remains within budget.

**Acceptance Criteria:**
- [ ] Only the approved Amazon Bedrock Claude Haiku model is permitted; Sonnet and Opus identifiers are rejected.
- [ ] Duplicate analyses for the same `{repo}/{pr}/{head_sha}` are never re-invoked after an S3 analysis-cache hit.
- [ ] CloudWatch alarm fires if daily Bedrock invocations exceed 100.
- [ ] S3 diff and analysis objects expire after 7 days.
- [ ] Cache keys remain SHA-based in v1: identical diff content in different PRs is cached separately.

## Non-Functional Requirements

- The synchronous end-to-end review path completes within 30 seconds at p95 under normal service availability; upstream GitHub throttling, Bedrock outages, and other external failures are excluded from this latency target. Lambda timeout remains 30 seconds.
- Lambda cold-start initialization completes within 5 seconds at p95 under normal deployment conditions.
- Emit structured JSON logs to CloudWatch for every accepted event. Each pipeline record includes `pr_url`, `repo`, `action`, `head_sha`, `review_id` (or null when no review is created), and `status`.
- Emit CloudWatch metrics using the `ReviewCompleted` and `ReviewFailed` metric names, with dimensions `repo` and `severity_count`.
- The test suite makes zero real AWS calls; AWS integrations use moto or equivalent mocks.
- Core modules maintain at least 80% automated test coverage.
- Webhook authentication uses HMAC-SHA256 without timestamp-based replay protection in v1. Replay protection is a known limitation deferred to `design.md`.

## Medium/Low Decisions

- **C2 — Content-hash deduplication:** Keep the current `{repo}/{pr}/{head_sha}` key. Cross-PR content-hash deduplication is deferred to `design.md`.
- **A2 — Async wording:** Resolved by the synchronous execution decision in US-1; no asynchronous fallback is implied.
- **A4 — Reused commit SHA after force-push:** Accepted v1 behavior. If the same head SHA reappears, the cache may be reused and the system will not re-analyze it.
- **A6 — 30-second target:** Defined as a p95 target under normal service availability in the NFRs.
- **V2 — GitHub rate limits:** Folded into US-4: detect exhausted-rate-limit HTTP 403, attempt one neutral comment, and record failure without unbounded retries.
- **V6 — Per-PR/repository cost tracing:** Out of scope for v1 and deferred to `design.md`/cost-governance work.
- **V7 — Replay protection:** Accepted as a known v1 limitation and deferred to `design.md`.
- **E1 — Empty PR:** Folded into US-2 with a neutral `No changes to review` comment.
- **E2 — Oversized diff:** Folded into US-3 with hunk-boundary truncation at 100 eligible hunks and an explicit truncation note.
- **E3 — Model deprecation/configuration:** Folded into US-3 with an environment override that defaults to Haiku and fails closed for non-Haiku models.
- **E4 — Invalid signature:** Folded into US-1; invalid or missing signatures return HTTP 401.
- **E6 — Seven-day cache expiry:** Accepted v1 trade-off and specified in US-5; long-lived PR cache behavior is deferred to `design.md`.
- **E7 — Multi-line findings:** Keep the single `line: int` schema in v1; `start_line`/`end_line` support is deferred to `design.md`.
- **E8 — Repository visibility:** Folded into US-4; the GitHub App token must support the required permissions for both public and private repositories.
- **E9 — Non-positive lines:** Folded into US-3; findings with `line < 1` are dropped before publication.
