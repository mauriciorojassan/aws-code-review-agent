# Code Review Agent 🤖

[![CI](https://github.com/mauriciorojassan/hackkiroaws/actions/workflows/ci.yml/badge.svg)](https://github.com/mauriciorojassan/hackkiroaws/actions/workflows/ci.yml)

Automated GitHub PR reviewer powered by Amazon Bedrock (Claude 3 Haiku).

## What it does

1. Receives GitHub PR webhooks (`pull_request` events: `opened`, `synchronize`, `reopened`).
2. Fetches the diff via the GitHub REST API.
3. Analyzes changes with Bedrock Claude 3 Haiku (`anthropic.claude-3-haiku-20240307`).
4. Posts a summary + inline review comments back to the PR.

## Architecture

```
GitHub → API Gateway HTTP API v2 → Lambda → Bedrock (Haiku)
                                       ↕
                                   S3 (diff + analysis cache)
                                   Secrets Manager (webhook secret + token)
```

## Quick Start

```bash
# 1. Install project + dev dependencies (editable)
pip install -e ".[dev]"

# 2. Run the local gate — same bundle CI runs
pytest --cov=code_review_agent --cov-fail-under=98
ruff check src/ tests/ mcp_server/
black --check src/ tests/ mcp_server/
sam validate --lint
```

### Smoke-test the handler locally

See [`docs/smoke-test.md`](docs/smoke-test.md) — direct-Python invocation,
`sam build`, and `sam local invoke` walkthrough with a documented Docker
API-drift caveat.

### Deploy to AWS

See [`docs/deployment.md`](docs/deployment.md) — `sam deploy --guided`
walkthrough with parallel Personal Access Token (simple) and GitHub App
(organization-scoped) auth paths.

## Project Structure

See [`.kiro/steering/structure.md`](.kiro/steering/structure.md) for the full
layout.

## Cost Governance

Designed to stay under $3/month at typical PR volumes. See
[`.kiro/steering/aws-cost-governance.md`](.kiro/steering/aws-cost-governance.md)
for the cost model (local-only, gitignored).

## MCP Server (optional / future)

`mcp_server/` contains a scaffold for exposing the PR-review tooling over the
Model Context Protocol. Not required for the Lambda flow above; see
`.kiro/specs/cra-001/tasks.md` Wave 5 for status.

## License

MIT
