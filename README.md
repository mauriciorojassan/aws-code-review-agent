# AWS Code Review Agent

[![CI](https://github.com/mauriciorojassan/aws-code-review-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/mauriciorojassan/aws-code-review-agent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Open in Codespaces](https://img.shields.io/badge/Open%20in-Codespaces-black?logo=github)](https://github.com/codespaces/new?repo=mauriciorojassan/aws-code-review-agent&ref=main)

> Self-hosted GitHub bot that reviews pull-request diffs using AWS Bedrock, with data residency and anti-hallucination guards.

## Quick Start

### Prerequisites

- Python 3.12+
- AWS account with Bedrock access
- GitHub PAT or App installation token

### Install and run

```bash
pip install -e ".[dev]"
pytest --cov=code_review_agent --cov-fail-under=99
ruff check src/ tests/ mcp_server/
black --check src/ tests/ mcp_server/
sam validate --lint
```

Deploy:

```bash
sam build && sam deploy --guided
# Add the webhook URL to the repo with pull_request events.
```

## Architecture

A serverless GitHub webhook consumer built on AWS SAM. API Gateway forwards `pull_request` events to a Lambda that validates the HMAC signature, fetches the diff, filters noise, sends chunks to Amazon Bedrock Claude Haiku, and posts summary and inline comments back to GitHub.

Diffs and analyses are cached in S3 with a 7-day lifecycle to avoid re-processing and to keep costs low. Secrets live in AWS Secrets Manager.

See [`docs/adr/001-initial-architecture.md`](docs/adr/001-initial-architecture.md) for the primary architectural decision and trade-offs.

## Tech Stack

| Layer | Choice |
|-------|--------|
| Compute | AWS Lambda + SAM |
| HTTP | API Gateway HTTP API v2 |
| Model | Amazon Bedrock — Claude Haiku 4.5 |
| Cache | S3 with lifecycle policy |
| Secrets | AWS Secrets Manager |
| Quality | pytest, ruff, black, `sam validate` |

## License

MIT
