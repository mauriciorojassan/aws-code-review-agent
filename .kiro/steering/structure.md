# Structure — Code Review Agent

## Directory Layout

```
code-review-agent/
├── .github/
│   └── workflows/
│       └── ci.yml                  # GitHub Actions gate (Wave 6, T6.5)
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
│   ├── code_review_agent/
│   │   ├── __init__.py
│   │   ├── handler.py              # Lambda entry point — 16-step synchronous pipeline
│   │   ├── credentials.py          # Secrets Manager + env-first fallback (Wave 4)
│   │   ├── github_client.py        # GitHub diff fetch with retry + rate-limit detect
│   │   ├── webhook_validator.py    # HMAC-SHA256 signature + event/action filters
│   │   ├── diff_cache.py           # S3 get/put for diffs and analyses (lazy client)
│   │   ├── diff_filter.py          # Denylist + binary detection → EligibleDiff
│   │   ├── diff_parser.py          # Unified-diff hunk parser + finding validator
│   │   ├── reviewer.py             # Bedrock Haiku invocation + response parsing
│   │   ├── review_publisher.py     # GitHub review POST with dedup, rate-limit, F8 escape
│   │   ├── observability.py        # Structured logs + CloudWatch metrics
│   │   └── models.py               # Pydantic models (webhook, findings)
│   └── requirements.txt            # Runtime deps for SAM deployment package
├── mcp_server/
│   ├── __init__.py
│   └── server.py                   # MCP stdio server (optional, deferred in v1)
├── tests/
│   ├── conftest.py                 # moto + AWS env fixtures, sample diff/findings
│   ├── fixtures/
│   │   └── sample_webhook_payload.json
│   ├── test_credentials.py
│   ├── test_diff_cache.py
│   ├── test_diff_filter.py
│   ├── test_diff_parser.py
│   ├── test_github_client.py
│   ├── test_handler.py
│   ├── test_models.py
│   ├── test_observability.py
│   ├── test_review_publisher.py
│   ├── test_reviewer.py
│   └── test_webhook_validator.py
├── docs/
│   ├── smoke-test.md               # Local invocation runbook (T4.4)
│   └── deployment.md               # SAM deploy walkthrough with PAT + App paths (T6.4)
├── events/
│   ├── pr_opened.json              # Signed HMAC full-pipeline fixture
│   ├── pr_closed_filtered.json     # Signed HMAC fast-filter fixture
│   └── env.json                    # env-var overrides for `sam local invoke`
├── template.yaml                   # SAM template (API Gateway, Lambda, S3, alarms)
├── mise.toml                       # Python 3.12 pin (Lambda runtime match)
├── pyproject.toml                  # Project metadata, ruff/black/pytest config
├── requirements.txt                # Root/dev deps (also see pyproject `.[dev]`)
├── README.md
└── .gitignore
```

## Module Responsibilities

| Module | Role |
|--------|------|
| `handler.py` | Lambda entry point — 16-step synchronous pipeline: signature check → JSON parse → event/action filter → diff fetch (with cache) → eligibility filter → Bedrock analysis (with cache) → finding validation → publish. Uniform response shape, uniform failure emission (`ReviewFailed` metric + structured log). Empty-webhook-secret → 401, non-Haiku model config → 500 `model_config_error`, malformed base64 → 401 (all Wave 4 JD F1/F2/F3 fixes). |
| `credentials.py` | Secrets Manager fetch with env-first fallback. `get_webhook_secret() → bytes`, `get_github_token() → str \| None`. Lazy singleton boto3 client + payload cache. Fail-quiet on all Secrets Manager errors; failed fetches cache empty to prevent retry storms. |
| `github_client.py` | `fetch_pr_diff(repo, pr, *, client, token) → str`. Raises `RateLimitError` (subclass of `GitHubFetchError`) on 403+`X-RateLimit-Remaining:0`, 403/429+`Retry-After`, or bare 429. Retries once on 5xx + transport errors; 4xx (non-rate-limit) raises immediately. |
| `webhook_validator.py` | HMAC-SHA256 signature check (timing-safe), `X-GitHub-Event` filter, action filter (`opened`, `synchronize`, `reopened`). |
| `diff_cache.py` | S3 get/put for diffs (`diffs/{repo}/{pr}/{sha}.diff`) and analyses (`analyses/{repo}/{pr}/{sha}.json`). Lazy boto3 client, call-time env resolution for `DIFF_CACHE_BUCKET`. |
| `diff_filter.py` | Eligibility filter with denylist (`*.lock`, `*.min.*`, package/yarn/poetry/Cargo locks) and NUL/UTF-8 binary detection. Returns `EligibleDiff` with counts and `too_large`/`is_empty` flags. |
| `diff_parser.py` | Parses `@@` hunk headers into post-state line ranges; validates each finding's file+line against the parsed ranges. |
| `reviewer.py` | Bedrock invocation. Enforces Haiku-only `model_id` (regex-gated, accepts Haiku 3.x family + Haiku 4.5 foundation and `us.`/`global.` cross-region inference profile ids), honors `BEDROCK_MODEL_ID` env override, lazy singleton client with Lambda-safe timeouts (`read_timeout=25`, `connect_timeout=3`, `max_attempts=1`). Robust `_parse_findings` that never raises. |
| `review_publisher.py` | Single GitHub review post per PR. Dedup marker `<!-- cra-dedup: {head_sha} -->`, severity-priority sort, 20-inline cap with overflow rendered in a fenced body block. Rate-limit detection matches `github_client.py`. Single retry on other failures. Neutral-comment fallback via `post_issue_comment` (never raises). **F8: `_escape_markdown` strips triple-backticks and leading pipes from LLM-generated `message` / `suggestion` at render time in both `_finding_to_comment` and `_build_review_body`.** |
| `observability.py` | `emit_structured_log(...)` — JSON log record with required schema fields; captured on stdout by the Lambda runtime → CloudWatch Logs. `emit_metric(...)` — CloudWatch `put_metric_data` with lazy singleton client, fail-silent on `ClientError`/`BotoCoreError`. |
| `models.py` | Pydantic schemas: `Finding` (with `line: int = Field(gt=0)`), `WebhookPayload` (with `extra="ignore"`), `RepoInfo`, `PRInfo`, `HeadInfo`, `OwnerInfo`, `ReviewResult`. |
| `mcp_server/server.py` | MCP stdio server exposing `read_pr_diff` and `post_review_comment`. Optional in v1 — the Lambda handler calls GitHub directly via httpx. |

## Test Layout

Every `src/code_review_agent/*.py` module (except `__init__.py`) has a peer `tests/test_*.py` file. Coverage is 100% on all Wave 1–4 src modules except `diff_cache.py` (88%, exception paths not exercised).

## Configuration Boundaries

- **Environment variables** (resolved at call time so tests can monkeypatch): `DIFF_CACHE_BUCKET`, `BEDROCK_MODEL_ID`, `WEBHOOK_SECRET`, `GITHUB_TOKEN`, `SECRETS_ARN`, `METRICS_NAMESPACE`.
- **Env-first credential resolution**: `WEBHOOK_SECRET` and `GITHUB_TOKEN` env vars *win* over Secrets Manager. Enables local dev + `sam local invoke` without touching AWS. Production deploys leave both unset and rely on the Secrets Manager payload keyed under `webhook_secret` + `github_token`.
- **Workspace settings**: `.kiro/settings/cli.json` sets `chat.defaultAgent = "reviewer"` so the reviewer agent activates automatically for `kiro-cli chat` in this directory.
- **CI**: `.github/workflows/ci.yml` runs the same gate we run locally (ruff, black --check, pytest --cov-fail-under=98, sam validate --lint) on Python 3.12 on every push + PR to `main`.

## Runbooks

- **`docs/smoke-test.md`** — direct-Python invocation, `sam validate`, `sam build`, `sam local invoke`. Documents a known SAM CLI ↔ Docker API-drift caveat on the dev host.
- **`docs/deployment.md`** — `sam deploy --guided` walkthrough with parallel PAT and GitHub App auth paths. Includes rollback, teardown, and a "Future — auto-refresh in Lambda" pointer for the deferred JWT exchange.
