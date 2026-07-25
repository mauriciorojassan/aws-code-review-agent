# Structure — Code Review Agent

## Directory Layout

```
code-review-agent/
├── .kiro/
│   ├── settings/
│   │   └── cli.json                # Workspace CLI settings (activates reviewer agent)
│   ├── steering/                   # Product, tech, structure, governance docs
│   │   ├── product.md
│   │   ├── tech.md
│   │   ├── structure.md
│   │   └── aws-cost-governance.md  # Gitignored — internal cost policy
│   ├── agents/
│   │   └── reviewer.json           # Local reviewer agent with cost + secret hooks
│   └── specs/cra-001/              # Feature spec (requirements → design → tasks)
│       ├── requirements.md
│       ├── design.md
│       └── tasks.md
├── src/
│   └── code_review_agent/
│       ├── __init__.py
│       ├── handler.py              # Lambda entry point (stub, Wave 4)
│       ├── webhook_validator.py    # HMAC-SHA256 signature + event/action filters
│       ├── diff_cache.py           # S3 get/put for diffs and analyses (lazy client)
│       ├── diff_filter.py          # Denylist + binary detection → EligibleDiff
│       ├── diff_parser.py          # Unified-diff hunk parser + finding validator
│       ├── reviewer.py             # Bedrock Haiku invocation + response parsing
│       ├── review_publisher.py     # GitHub review POST with dedup + rate-limit
│       ├── observability.py        # Structured logs + CloudWatch metrics
│       └── models.py               # Pydantic models (webhook, findings)
├── mcp_server/
│   ├── __init__.py
│   └── server.py                   # MCP stdio server (optional, deferred in v1)
├── tests/
│   ├── conftest.py                 # moto + AWS env fixtures, sample diff/findings
│   ├── fixtures/
│   │   └── sample_webhook_payload.json
│   ├── test_diff_cache.py
│   ├── test_diff_filter.py
│   ├── test_diff_parser.py
│   ├── test_models.py
│   ├── test_observability.py
│   ├── test_review_publisher.py
│   ├── test_reviewer.py
│   └── test_webhook_validator.py
├── template.yaml                   # SAM template (API Gateway, Lambda, S3, alarms)
├── pyproject.toml                  # Project metadata, ruff/black/pytest config
├── requirements.txt                # Lambda runtime deps
├── README.md
└── .gitignore
```

## Module Responsibilities

| Module | Role |
|--------|------|
| `handler.py` | Lambda entry point — receives webhook, orchestrates the synchronous pipeline. Stub in v1; full orchestration is Wave 4 (T4.1). |
| `webhook_validator.py` | HMAC-SHA256 signature check (timing-safe), `X-GitHub-Event` filter, action filter (`opened`, `synchronize`). |
| `diff_cache.py` | S3 get/put for diffs (`diffs/{repo}/{pr}/{sha}.diff`) and analyses (`analyses/{repo}/{pr}/{sha}.json`). Lazy boto3 client, call-time env resolution for `DIFF_CACHE_BUCKET`. |
| `diff_filter.py` | Eligibility filter with denylist (`*.lock`, `*.min.*`, package/yarn/poetry/Cargo locks) and NUL/UTF-8 binary detection. Returns `EligibleDiff` with counts and `too_large`/`is_empty` flags. |
| `diff_parser.py` | Parses `@@` hunk headers into post-state line ranges; validates each finding's file+line against the parsed ranges. |
| `reviewer.py` | Bedrock invocation. Enforces Haiku-only `model_id` (regex-gated), honors `BEDROCK_MODEL_ID` env override, lazy singleton client with Lambda-safe timeouts (`read_timeout=25`, `connect_timeout=3`, `max_attempts=1`). Robust `_parse_findings` that never raises. |
| `review_publisher.py` | Single GitHub review post per PR. Dedup marker `<!-- cra-dedup: {head_sha} -->`, severity-priority sort, 20-inline cap with overflow rendered in a fenced body block. Rate-limit detection on 403+`X-RateLimit-Remaining:0`, 403/429+`Retry-After`, or bare 429. Single retry on other failures. |
| `observability.py` | `emit_structured_log(...)` — JSON log record with required schema fields; captured on stdout by the Lambda runtime → CloudWatch Logs. `emit_metric(...)` — CloudWatch `put_metric_data` with lazy singleton client, fail-silent on `ClientError`/`BotoCoreError`. |
| `models.py` | Pydantic schemas: `Finding` (with `line: int = Field(gt=0)`), `WebhookPayload` (with `extra="ignore"`), `RepoInfo`, `PRInfo`, `HeadInfo`, `OwnerInfo`, `ReviewResult`. |
| `mcp_server/server.py` | MCP stdio server exposing `read_pr_diff` and `post_review_comment`. Optional in v1 — the Lambda handler is designed to call GitHub directly via httpx. |

## Test Layout

Every `src/code_review_agent/*.py` module (except `__init__.py` and the Wave 4 `handler.py` stub) has a peer `tests/test_*.py` file. Coverage is 100% on all Wave 1–3 src modules except `diff_cache.py` (88%, exception paths not exercised).

## Configuration Boundaries

- **Environment variables** (resolved at call time so tests can monkeypatch): `DIFF_CACHE_BUCKET`, `BEDROCK_MODEL_ID`, `GITHUB_TOKEN`, `METRICS_NAMESPACE`, `SECRETS_ARN` (Wave 4).
- **Workspace settings**: `.kiro/settings/cli.json` sets `chat.defaultAgent = "reviewer"` so the reviewer agent activates automatically for `kiro-cli chat` in this directory.
- **Secrets**: fetched from AWS Secrets Manager at Lambda cold start (Wave 4, T4.3). Never in env vars or source.
