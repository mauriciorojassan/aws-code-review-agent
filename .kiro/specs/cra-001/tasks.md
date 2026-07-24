# CRA-001: Automated PR Review Pipeline — Tasks

## Wave 1: Foundation (no external dependencies)

- [ ] **T1.1** Create `src/code_review_agent/models.py` — Pydantic models for Finding, WebhookPayload, ReviewResult.
- [ ] **T1.2** Create `src/code_review_agent/diff_cache.py` — S3 get/put for diffs and analyses.
- [ ] **T1.3** Create `tests/test_diff_cache.py` — Unit tests with moto S3 mock.
- [ ] **T1.4** Create `template.yaml` — SAM template with Lambda, API Gateway, S3 bucket, IAM roles.

## Wave 2: Core Logic (depends on Wave 1)

- [ ] **T2.1** Create `src/code_review_agent/reviewer.py` — Bedrock prompt construction + invocation + parsing.
- [ ] **T2.2** Create `tests/test_reviewer.py` — Unit tests mocking Bedrock responses.
- [ ] **T2.3** Create `src/code_review_agent/handler.py` — Lambda handler with webhook validation, orchestration.
- [ ] **T2.4** Create `tests/test_handler.py` — Integration tests for the full handler flow.

## Wave 3: MCP & Integration (depends on Wave 2)

- [ ] **T3.1** Create `mcp_server/server.py` — MCP stdio server with `read_pr_diff` and `post_review_comment` tools.
- [ ] **T3.2** Create `tests/test_mcp_server.py` — MCP tool unit tests.
- [ ] **T3.3** Wire handler to use MCP tools (or direct GitHub calls as fallback).
- [ ] **T3.4** End-to-end test: simulated webhook → cached analysis → mock GitHub comment.

## Wave 4: Deploy & Harden (depends on Wave 3)

- [ ] **T4.1** Add Secrets Manager resource to template.yaml + code to fetch at cold start.
- [ ] **T4.2** Add CloudWatch alarm for Bedrock invocation count.
- [ ] **T4.3** Add S3 lifecycle rule (7-day expiry) to template.yaml.
- [ ] **T4.4** `sam build && sam local invoke` smoke test.
- [ ] **T4.5** Write deployment runbook in README.md.

## Definition of Done
- All tests pass (`pytest --cov` ≥ 80%).
- `ruff check` and `black --check` pass with zero findings.
- `sam validate` passes on template.yaml.
- Cost governance constraints verified (Haiku only, caching active, alarms configured).
