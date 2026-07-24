# CRA-001: Automated PR Review Pipeline — Requirements

## User Stories

### US-1: Webhook Reception
**As a** repository maintainer,
**I want** the system to automatically receive GitHub PR events,
**So that** reviews begin without manual intervention.

**Acceptance Criteria:**
- [ ] System exposes an HTTPS endpoint that accepts GitHub webhook POST requests.
- [ ] Validates `X-Hub-Signature-256` header to authenticate GitHub origin.
- [ ] Handles `pull_request.opened` and `pull_request.synchronize` events.
- [ ] Returns 200 within 3 seconds (async processing if needed).

### US-2: Diff Retrieval
**As a** the review pipeline,
**I want** to fetch the full PR diff via MCP,
**So that** analysis has complete file-change context.

**Acceptance Criteria:**
- [ ] MCP tool `read_pr_diff` fetches diff from GitHub API.
- [ ] Returns unified diff format with file paths.
- [ ] Handles PRs with up to 50 changed files gracefully.
- [ ] Caches diff in S3 keyed by `{repo}/{pr}/{head_sha}.diff`.

### US-3: AI Analysis
**As a** developer receiving a review,
**I want** the analysis to be specific and actionable,
**So that** I can address issues quickly without guessing.

**Acceptance Criteria:**
- [ ] Sends diff to Bedrock Claude Haiku with structured prompt.
- [ ] Response schema: list of findings with `file`, `line`, `severity`, `message`, `suggestion`.
- [ ] Severity levels: `error`, `warning`, `info`.
- [ ] Skips analysis if identical diff was already reviewed (cache hit).

### US-4: Comment Publishing
**As a** developer,
**I want** findings posted as inline PR comments,
**So that** I see them in context on the relevant lines.

**Acceptance Criteria:**
- [ ] MCP tool `post_review_comment` creates GitHub PR review with inline comments.
- [ ] Maps findings to correct file path and diff position.
- [ ] Groups all findings into a single review submission (not individual comments).
- [ ] Includes a summary comment with counts per severity.

### US-5: Cost Governance
**As a** project owner,
**I want** automated cost controls,
**So that** the system never exceeds budget.

**Acceptance Criteria:**
- [ ] Only `anthropic.claude-3-haiku` model is used.
- [ ] Duplicate diffs are never re-analyzed (S3 cache dedup).
- [ ] CloudWatch alarm fires if daily Bedrock invocations exceed 100.
- [ ] S3 objects expire after 7 days.

## Non-Functional Requirements
- Cold start latency < 5 seconds.
- End-to-end review posted within 30 seconds of webhook receipt.
- Zero real AWS calls in test suite (moto).
- 80%+ code coverage on core modules.
