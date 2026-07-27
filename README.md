# Code Review Agent

[![CI](https://github.com/mauriciorojassan/aws-code-review-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/mauriciorojassan/aws-code-review-agent/actions/workflows/ci.yml)

Automated GitHub PR reviewer on AWS: webhook in → Bedrock analysis → inline review comments out.

Built with **Kiro** + **AWS SAM** for the Código Facilito × AWS hackathon.

## Live demo

Open this PR and scroll the bot review + inline comments:

**https://github.com/mauriciorojassan/cra-demo-target/pull/1**

The bot caught SQL injection, off-by-one, mutable defaults, and bare `except` on a deliberately buggy sample service.

## Problem → solution

| Pain | What this does |
|------|----------------|
| Small teams wait hours for a first human review | Posts a structured first-pass in ~10–15s after PR open/update |
| Review quality depends on who is online | Same Haiku-backed checklist every time |
| LLM cost and blast radius | Haiku-only gate, S3 analysis cache, >50-file skip, CloudWatch alarm |

## How it works

```
GitHub PR webhook
    → API Gateway HTTP API v2
    → Lambda (Python 3.12, arm64)
         → validate HMAC (webhook secret)
         → fetch diff (GitHub REST)
         → filter noise (locks, binaries, denylist)
         → Bedrock Claude Haiku 4.5 (cross-region inference profile)
         → post summary + inline comments
    ↔ S3 (diff + analysis cache, 7-day lifecycle)
    ↔ Secrets Manager (webhook secret + GitHub token)
```

## Stack

| Layer | Choice |
|-------|--------|
| Compute | AWS Lambda + SAM |
| HTTP | API Gateway HTTP API v2 |
| Model | Amazon Bedrock — Claude Haiku 4.5 (`us.anthropic.claude-haiku-4-5-20251001-v1:0`) |
| Cache | S3 |
| Secrets | Secrets Manager |
| Auth to GitHub | Fine-grained PAT or GitHub App installation token |
| Quality | 313 tests, ~100% `src` coverage, ruff + black + `sam validate` in CI |

## Quick start (local)

```bash
pip install -e ".[dev]"
pytest --cov=code_review_agent --cov-fail-under=99
ruff check src/ tests/ mcp_server/
black --check src/ tests/ mcp_server/
sam validate --lint
```

- Local handler smoke: [`docs/smoke-test.md`](docs/smoke-test.md)
- Deploy runbook (PAT + GitHub App paths): [`docs/deployment.md`](docs/deployment.md)

## Deploy (short path)

```bash
sam build && sam deploy --guided   # stack outputs WebhookUrl
# put webhook_secret + github_token into Secrets Manager
# add repo webhook → WebhookUrl, events: pull_request only
```

Designed to stay under **~$3/month** at typical PR volume (Haiku-only + cache + alarm).

## Project layout

See [`.kiro/steering/structure.md`](.kiro/steering/structure.md).

## License

MIT
