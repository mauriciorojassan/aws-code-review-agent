# Product — Code Review Agent

## Vision
An automated code reviewer that analyzes GitHub pull requests and posts actionable, inline comments — reducing review latency and catching issues before human reviewers engage.

## Core Value Proposition
- **Speed**: Reviews posted within seconds of PR open/update.
- **Consistency**: Every PR gets the same baseline checks (bugs, security, meaningful maintainability risks).
- **Cost-efficiency**: Runs on Bedrock Claude Haiku with an S3 analysis cache to stay well within free-tier credits.

## Target Users
- Solo developers and small teams using GitHub who want faster feedback loops.

## Key Workflows

The Lambda performs the complete pipeline synchronously within a single invocation. See `.kiro/specs/cra-001/design.md` §1 for the full step list.

1. **Webhook reception** — GitHub sends a `pull_request` webhook to API Gateway → Lambda. Signature is validated (HMAC-SHA256); non-`pull_request` events and non-`opened`/`synchronize` actions are filtered as deterministic 200 no-ops.
2. **Diff retrieval** — Lambda fetches the unified diff directly from the GitHub REST API using an httpx client authenticated with a GitHub App token from Secrets Manager. The complete diff is cached in S3 (`diffs/{repo}/{pr}/{sha}.diff`).
3. **Eligibility filtering** — Denylisted files (`*.lock`, `*.min.*`, `package-lock.json`, `yarn.lock`, `poetry.lock`, `Cargo.lock`) and binary files (NUL byte / UTF-8 failure) are excluded before the prompt is built. PRs with >50 eligible files or 0 files short-circuit to a neutral comment without invoking Bedrock.
4. **AI analysis** — Eligible diff is sent to Amazon Bedrock (Claude Haiku, model id enforced by regex + env override). Response is parsed into `Finding` objects; findings whose line falls outside a valid `+` hunk are dropped and the review is skipped as a data-quality no-op.
5. **Review publication** — Findings are posted as a single GitHub PR review (`POST /repos/{owner}/{repo}/pulls/{pr}/reviews`) with up to 20 inline comments and any overflow in a fenced body block. A dedup marker containing the head SHA prevents double-posting across webhook redeliveries.
6. **Observability** — Every accepted event emits a JSON log record and a `ReviewCompleted` / `ReviewFailed` CloudWatch metric.

## Non-Goals (v1)
- Full repo-level architectural analysis.
- Multi-model routing (Sonnet / Opus fallback).
- Self-hosted GitHub Enterprise support.
- Cross-PR content-hash deduplication (cache keys stay `{repo}/{pr}/{sha}` in v1).
- Replay-protected webhooks (HMAC-only; no timestamp / nonce state).
- Multi-line finding ranges (single `line: int` schema).

## MCP Server — Deferred

An MCP stdio server exposing `read_pr_diff` and `post_review_comment` lives in `mcp_server/` and is fully wired for tool listing. It is **not** on the v1 execution path — the Lambda handler calls GitHub directly via httpx. MCP is retained as a scaffold for future integrations (local CLI reviewer, IDE tooling) but no runtime code depends on it.

## Success Signals (v1)

- End-to-end latency < 30s at p95 on a typical PR (US-1 NFR).
- Zero real AWS calls in the test suite (moto everywhere, including Secrets Manager).
- ≥ 80% test coverage on core modules (currently 100% on all Wave 1–4 src modules except `diff_cache.py` at 88%; 98.56% src total).
- CloudWatch alarm on daily Bedrock invocation count > 100 never fires under normal development traffic.
