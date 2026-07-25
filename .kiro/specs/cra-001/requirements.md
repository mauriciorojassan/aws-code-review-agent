# CRA-001: Automated PR Review Pipeline — Requirements

## Scope and Execution Decisions

- **Execution model:** v1 is fully synchronous. The Lambda performs retrieval, analysis, validation, and publication during one invocation and returns only after the pipeline reaches a successful, deterministic no-op, or failure outcome. There is no queue or second-stage worker in v1.
- **Reviewability limit:** The complete diff is cached, but only eligible files are sent to Bedrock. A PR with more than 50 eligible changed files is rejected as too large for v1.
- **Inline publication policy:** Findings are validated before publication, and GitHub publication is atomic. The system never posts a partial review.
- **Finding location:** `line` is an absolute line number in the post-state (new) file, as required by the GitHub review API. It is not a diff-relative line number.

## Acceptance Criteria Notation

Every acceptance criterion below is written in **EARS** (Easy Approach to Requirements Syntax). The five templates in use:

- **Ubiquitous:** `THE SYSTEM SHALL <action>` — always-on obligations.
- **Event-driven:** `WHEN <trigger>, THE SYSTEM SHALL <action>` — reactive behavior tied to a discrete event.
- **State-driven:** `WHILE <state>, THE SYSTEM SHALL <action>` — behavior tied to a sustained condition.
- **Optional feature:** `WHERE <feature>, THE SYSTEM SHALL <action>` — behavior tied to a configuration or feature flag.
- **Unwanted behavior:** `IF <trigger>, THEN THE SYSTEM SHALL <action>` — guard clauses for error / edge cases.

Each AC is independently testable. The word `SHALL` is normative; `SHALL NOT` is a prohibition; `SHOULD` and `MAY` are not used in v1 ACs.

## User Stories

### US-1: Webhook Reception

**As a** repository maintainer,
**I want** the system to automatically receive GitHub PR events,
**So that** reviews begin without manual intervention.

**Acceptance Criteria:**
- [ ] THE SYSTEM SHALL expose an HTTPS endpoint that accepts GitHub webhook POST requests.
- [ ] WHEN a webhook request arrives, THE SYSTEM SHALL validate the `X-Hub-Signature-256` header against the configured webhook secret using HMAC-SHA256 in constant time.
- [ ] IF the `X-Hub-Signature-256` header is missing, malformed, or does not match the computed HMAC, THEN THE SYSTEM SHALL return HTTP 401 and SHALL NOT invoke any downstream pipeline stage.
- [ ] IF the `X-GitHub-Event` header value is not `pull_request`, THEN THE SYSTEM SHALL return HTTP 200 without invoking analysis or publication.
- [ ] IF the webhook `action` field is not `opened` or `synchronize`, THEN THE SYSTEM SHALL return HTTP 200 without invoking analysis or publication, and SHALL NOT delete or mutate any cache entry.
- [ ] WHEN a webhook event passes signature, event-header, and action filters, THE SYSTEM SHALL execute the complete pipeline synchronously within a single Lambda invocation.
- [ ] WHEN the pipeline completes with a successful review publication or a deterministic no-op outcome, THE SYSTEM SHALL return HTTP 200.
- [ ] IF the pipeline encounters a transient processing failure (GitHub 5xx during diff fetch, Bedrock timeout, GitHub 5xx during review post), THEN THE SYSTEM SHALL return a non-2xx status so GitHub retries the delivery.

### US-2: Diff Retrieval and Scope Control

**As** the review pipeline,
**I want** to fetch and cache the complete PR diff while excluding unsuitable files,
**So that** analysis has relevant file-change context without uncontrolled token cost.

**Acceptance Criteria:**
- [ ] WHEN a PR event is accepted for processing, THE SYSTEM SHALL fetch the complete PR diff from the GitHub REST API.
- [ ] THE SYSTEM SHALL retrieve the diff in unified format including file paths and hunk metadata.
- [ ] WHEN the complete diff has been retrieved, THE SYSTEM SHALL cache it in S3 at `diffs/{repo}/{pr}/{head_sha}.diff` before invoking Bedrock analysis.
- [ ] WHEN building the Bedrock prompt, THE SYSTEM SHALL exclude any file whose basename matches `*.lock`, `*.min.*`, `package-lock.json`, `yarn.lock`, `poetry.lock`, or `Cargo.lock`.
- [ ] WHEN building the Bedrock prompt, THE SYSTEM SHALL exclude any file section that contains a NUL byte or fails UTF-8 decoding (binary detection heuristic).
- [ ] THE SYSTEM SHALL retain the complete, unfiltered diff in the S3 cache regardless of which sections were excluded from analysis.
- [ ] THE SYSTEM SHALL record the excluded-file count for the current PR and include it in the review summary body.
- [ ] IF a PR has 50 or fewer eligible files after exclusions, THEN THE SYSTEM SHALL proceed to Bedrock analysis.
- [ ] IF a PR has more than 50 eligible files after exclusions, THEN THE SYSTEM SHALL NOT invoke Bedrock, SHALL post exactly one neutral comment stating the PR is too large to review automatically, and SHALL return a deterministic no-op result with HTTP 200.
- [ ] IF a PR has zero eligible files after exclusions, THEN THE SYSTEM SHALL NOT invoke Bedrock and SHALL post exactly one neutral comment with body `No changes to review`.

### US-3: AI Analysis and Finding Validation

**As a** developer receiving a review,
**I want** the analysis to be specific, actionable, and safe to publish,
**So that** I can address issues quickly without guessing or receiving invalid comments.

**Acceptance Criteria:**
- [ ] WHEN the eligible diff is ready for analysis, THE SYSTEM SHALL invoke Amazon Bedrock with the model identifier resolved from the `BEDROCK_MODEL_ID` environment variable, defaulting to `anthropic.claude-3-haiku-20240307` when unset.
- [ ] IF the resolved model identifier does not match the Claude Haiku family (any of `anthropic.claude-3-haiku*`, `anthropic.claude-3-5-haiku*`, `anthropic.claude-3-7-haiku*`), THEN THE SYSTEM SHALL raise `ValueError` before invoking Bedrock.
- [ ] IF the resolved model identifier is empty, whitespace-only, or `None`, THEN THE SYSTEM SHALL raise `ValueError` before invoking Bedrock.
- [ ] IF the eligible diff exceeds the configured Bedrock context limit, THEN THE SYSTEM SHALL truncate at hunk boundaries to the first 100 eligible hunks AND SHALL add a machine-readable truncation note to both the analysis input and the review summary.
- [ ] THE SYSTEM SHALL parse Bedrock responses into a list of findings, each containing `file` (string), `line` (integer), `severity` (`error` | `warning` | `info`), `message` (string), and optional `suggestion` (string or null).
- [ ] THE SYSTEM SHALL treat `line` as an absolute 1-indexed line number in the post-state (new) file.
- [ ] THE SYSTEM SHALL reject any finding whose `line` value is less than 1 at the model boundary via `Field(gt=0)`.
- [ ] WHEN validating a finding, THE SYSTEM SHALL confirm that the finding's file exists in the eligible diff AND that its line falls within an added (`+`) hunk on the right side of the unified diff.
- [ ] IF any surviving finding is out-of-hunk after validation, THEN THE SYSTEM SHALL skip review publication entirely, emit a structured failure log, SHALL NOT submit any partial review, and SHALL return HTTP 200 (the fault is a Bedrock data-quality issue, not a transient infrastructure error).
- [ ] THE SYSTEM SHALL accept only `error`, `warning`, or `info` as severity values, defined as: `error` = correctness or security risk; `warning` = meaningful maintainability or reliability risk; `info` = non-blocking improvement.
- [ ] THE SYSTEM SHALL treat severity as informational only in v1 and SHALL NOT create a GitHub status check or merge block based on it.
- [ ] IF an S3 analysis-cache entry exists at `analyses/{repo}/{pr}/{head_sha}.json`, THEN THE SYSTEM SHALL reuse the cached findings and SHALL NOT invoke Bedrock.
- [ ] IF Bedrock returns a `ClientError` or `BotoCoreError`, THEN THE SYSTEM SHALL propagate the exception to the Lambda handler (which returns HTTP 500 so GitHub retries).
- [ ] IF a Bedrock response body is malformed (missing `content`, non-list content, missing / non-string / empty `text`, non-JSON `text`, non-array parsed JSON), THEN THE SYSTEM SHALL log a warning and return an empty findings list without raising.
- [ ] IF an individual finding element in a Bedrock response fails Pydantic validation, THEN THE SYSTEM SHALL log a warning, skip that element, and continue processing remaining elements.

### US-4: Comment and Review Publishing

**As a** developer,
**I want** findings posted as inline PR comments,
**So that** I see them in context on the relevant lines without duplicate or partial reviews.

**Acceptance Criteria:**
- [ ] WHEN publishing findings, THE SYSTEM SHALL create exactly one GitHub PR review per PR event, and SHALL NOT create one review per finding.
- [ ] THE SYSTEM SHALL limit each review to at most 20 inline comments.
- [ ] IF more than 20 valid findings exist, THEN THE SYSTEM SHALL select the first 20 for inline placement by severity priority (`error` before `warning` before `info`) then by file path then by line number, AND SHALL render the remaining findings in a fenced code block within the review body.
- [ ] THE SYSTEM SHALL include per-severity counts, the excluded-file count, and any truncation note in every review body.
- [ ] IF analysis produces zero valid findings, THEN THE SYSTEM SHALL post exactly one summary review stating that no actionable findings were detected, and SHALL NOT create any inline comments.
- [ ] IF any valid inline finding cannot be mapped by GitHub OR the review-submission request fails, THEN THE SYSTEM SHALL NOT post a partial review, SHALL emit a structured failure record, and SHALL return a non-success outcome.
- [ ] THE SYSTEM SHALL embed a deduplication marker of the form `<!-- cra-dedup: {head_sha} -->` in every generated review body.
- [ ] WHEN preparing to post a review, THE SYSTEM SHALL query existing bot-authored reviews for the same PR and inspect their bodies for the head-SHA dedup marker.
- [ ] IF a dedup marker for the current head SHA is found on an existing review, THEN THE SYSTEM SHALL skip posting and SHALL return a success outcome with `skipped_reason="dedup"`.
- [ ] IF GitHub responds with rate-limit exhaustion (HTTP 403 with `X-RateLimit-Remaining: 0`, HTTP 403 or 429 with a `Retry-After` header, or any bare HTTP 429), THEN THE SYSTEM SHALL skip review publication without retrying, attempt one neutral issue comment explaining the rate-limit exhaustion, emit a `ReviewFailed` CloudWatch metric, and return HTTP 200.
- [ ] IF GitHub returns any other failure (5xx, non-rate-limit 4xx, transport error), THEN THE SYSTEM SHALL retry the review post exactly once before returning `skipped_reason="github_error"`.
- [ ] THE SYSTEM SHALL authenticate GitHub calls with a GitHub App token that grants pull-request read and write permissions on both public and private repositories.

### US-5: Cost Governance

**As a** project owner,
**I want** automated cost controls,
**So that** the system avoids unnecessary model calls and remains within budget.

**Acceptance Criteria:**
- [ ] THE SYSTEM SHALL permit only Claude Haiku model identifiers for Bedrock invocations.
- [ ] IF a Sonnet, Opus, or any other non-Haiku model identifier is configured or requested at runtime, THEN THE SYSTEM SHALL raise `ValueError` before invoking Bedrock.
- [ ] IF an S3 analysis-cache entry exists for `{repo}/{pr}/{head_sha}`, THEN THE SYSTEM SHALL reuse those findings and SHALL NOT invoke Bedrock a second time for the same key.
- [ ] THE SYSTEM SHALL emit a CloudWatch alarm when the daily Bedrock invocation count exceeds 100.
- [ ] THE SYSTEM SHALL expire cached diff and analysis objects from S3 after 7 days via a bucket lifecycle rule.
- [ ] THE SYSTEM SHALL key cache entries by `{repo}/{pr}/{head_sha}` in v1; identical diff content in different PRs is cached separately.

## Non-Functional Requirements

- THE SYSTEM SHALL complete the synchronous end-to-end review path within 30 seconds at p95 under normal service availability. Upstream GitHub throttling, Bedrock outages, and other external failures are excluded from this latency target. Lambda timeout remains 30 seconds.
- THE SYSTEM SHALL complete Lambda cold-start initialization within 5 seconds at p95 under normal deployment conditions.
- WHEN an accepted event is processed, THE SYSTEM SHALL emit a structured JSON log record to CloudWatch containing at minimum `pr_url`, `repo`, `action`, `head_sha`, `review_id` (or `null` when no review is created), `status`, and `timestamp`.
- WHEN a review post succeeds, THE SYSTEM SHALL emit a `ReviewCompleted` CloudWatch metric with dimensions `repo` and `severity_count`.
- WHEN a review post fails or is skipped for rate-limit / out-of-hunk / GitHub-error reasons, THE SYSTEM SHALL emit a `ReviewFailed` CloudWatch metric with dimensions `repo` and `reason`.
- THE SYSTEM'S automated test suite SHALL make zero real AWS calls; all AWS integrations SHALL use moto or an equivalent mock.
- THE SYSTEM'S core modules under `src/code_review_agent/` SHALL maintain at least 80% automated line coverage.
- THE SYSTEM SHALL authenticate webhooks with HMAC-SHA256 only in v1; timestamp / nonce replay protection is a known deferred limitation documented in `design.md`.

## Medium/Low Decisions

- **C2 — Content-hash deduplication:** Keep the current `{repo}/{pr}/{head_sha}` key. Cross-PR content-hash deduplication is deferred to `design.md`.
- **A2 — Async wording:** Resolved by the synchronous execution decision in US-1; no asynchronous fallback is implied.
- **A4 — Reused commit SHA after force-push:** Accepted v1 behavior. If the same head SHA reappears, the cache may be reused and the system will not re-analyze it.
- **A6 — 30-second target:** Defined as a p95 target under normal service availability in the NFRs.
- **V2 — GitHub rate limits:** Folded into US-4: detect exhausted-rate-limit HTTP 403/429 (with `X-RateLimit-Remaining: 0` or `Retry-After`), attempt one neutral comment, and record failure without unbounded retries.
- **V6 — Per-PR/repository cost tracing:** Out of scope for v1 and deferred to `design.md`/cost-governance work.
- **V7 — Replay protection:** Accepted as a known v1 limitation and deferred to `design.md`.
- **E1 — Empty PR:** Folded into US-2 with a neutral `No changes to review` comment.
- **E2 — Oversized diff:** Folded into US-3 with hunk-boundary truncation at 100 eligible hunks and an explicit truncation note.
- **E3 — Model deprecation/configuration:** Folded into US-3 with an environment override that defaults to Haiku and fails closed for non-Haiku models.
- **E4 — Invalid signature:** Folded into US-1; invalid or missing signatures return HTTP 401.
- **E6 — Seven-day cache expiry:** Accepted v1 trade-off and specified in US-5; long-lived PR cache behavior is deferred to `design.md`.
- **E7 — Multi-line findings:** Keep the single `line: int` schema in v1; `start_line`/`end_line` support is deferred to `design.md`.
- **E8 — Repository visibility:** Folded into US-4; the GitHub App token must support the required permissions for both public and private repositories.
- **E9 — Non-positive lines:** Folded into US-3; findings with `line < 1` are rejected at the model layer via `Field(gt=0)`.
