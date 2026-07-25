# Tech — Code Review Agent

## Runtime
- Python 3.12+ (project targets `py312`; CI uses 3.13).
- AWS Lambda (ARM64, 256 MB, 30-second timeout).

## Infrastructure
- **AWS SAM** — `template.yaml` defines: Lambda function, API Gateway HTTP API, S3 bucket for diff/analysis cache with 7-day lifecycle rule, Secrets Manager entry for the GitHub App token + webhook secret, CloudWatch alarm on Bedrock invocation count > 100/day.

## AI / Models
- **Amazon Bedrock** — `anthropic.claude-3-haiku-20240307` (default). Model id is read from `BEDROCK_MODEL_ID` env var at call time; regex-gated to Claude Haiku family only (`^anthropic\.claude-3(-5|-7)?-haiku`). Sonnet, Opus, non-Anthropic, and ARN-embedded ids are rejected with `ValueError` before any Bedrock call.
- Prompt strategy: text response with an explicit JSON-array output contract in the system prompt. Response is parsed defensively — malformed shapes never propagate, individual invalid findings are skipped.
- Bedrock client is configured with Lambda-safe timeouts (`connect_timeout=3`, `read_timeout=25`, `max_attempts=1`) so a hanging call surfaces as a catchable `ReadTimeoutError` before Lambda's 30-second ceiling.

## GitHub Integration
- **Direct REST API via `httpx`** — sync `httpx.Client` in the Lambda handler / `review_publisher.py`. GitHub App token sourced from Secrets Manager at cold start (Wave 4).
- Rate-limit handling: 403 with `X-RateLimit-Remaining: 0` (primary), 403/429 with `Retry-After` (secondary), bare 429 (RFC 6585). Non-rate-limit failures retry exactly once.

## Agent Tooling (Optional / Deferred)
- **MCP Python SDK** — stdio transport server in `mcp_server/server.py` exposes `read_pr_diff` and `post_review_comment`. Not on the v1 execution path (see `product.md`). Retained for future local/IDE reviewer integrations.

## Quality & CI
- **pytest** + **pytest-cov** — 99% total coverage on Wave 1–3 modules; every src module has a peer `test_*.py`.
- **moto** — mocks all AWS calls (S3, CloudWatch, CloudWatch Logs). Zero real AWS traffic in tests.
- **httpx.MockTransport** — HTTP-level GitHub mocking for `review_publisher.py` tests.
- **ruff** — linter with `E, F, I, N, W, UP, S, B, A, C4, PT` rule sets enabled.
- **black** — formatter, `line-length=100`, `target-version=py312`.
- Test-suite gate: `pytest --cov` + `ruff check` + `black --check` before every commit.

## Key Libraries
- `boto3` — AWS SDK (S3, Bedrock Runtime, CloudWatch).
- `botocore` — used directly for `botocore.config.Config` and exception types (`ClientError`, `BotoCoreError`).
- `httpx` — sync HTTP client for GitHub REST API.
- `pydantic` — data validation. `Finding.line` uses `Field(gt=0)` to enforce the design invariant at the model layer.
- `mcp` — MCP Python SDK (for the optional MCP server; not runtime-critical).

## Conventions
- Type hints on every public function; `from __future__ import annotations` at the top of every module.
- One module per concern under `src/code_review_agent/`.
- Environment variables resolved at call time (not import time) so tests can `monkeypatch.setenv` in the fixture without reloading modules. Pattern established by `diff_cache._bucket_name()`.
- Lazy singleton boto3 clients per module (`diff_cache._client`, `reviewer._client`, `observability._client`). Module-level `_client: Any | None`, populated on first `get_*_client()` call. Safe under Lambda's single-threaded per-invocation model; tests reset via `monkeypatch`.
- Idempotent handler behavior: webhook redelivery for the same head SHA is a no-op via the dedup marker + S3 analysis cache.
- Observability is fail-silent: `emit_metric` swallows `ClientError` and `BotoCoreError`; non-botocore exceptions propagate as caller bugs.

## Local Development
```bash
# Install dev dependencies (editable + optional dev group)
pip install -e ".[dev]"

# Run the full gate
pytest --cov
ruff check src/ tests/ mcp_server/
black --check src/ tests/ mcp_server/

# Local Lambda invoke (once handler is wired in Wave 4)
sam build && sam local invoke ReviewFunction --event events/pr_opened.json
```
