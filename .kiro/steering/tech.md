# Tech — Code Review Agent

## Runtime
- Python 3.12
- AWS Lambda (ARM64, 256 MB, 30s timeout)

## Infrastructure
- AWS SAM (template.yaml)
- API Gateway HTTP API (webhook ingress)
- S3 bucket (diff cache / deduplication)
- Secrets Manager (GitHub App token)

## AI / Models
- Amazon Bedrock — `anthropic.claude-3-haiku-20240307`
- Prompt engineering with structured output (JSON schema)

## Agent Tooling
- MCP Python SDK (stdio transport)
- Tools: `read_pr_diff`, `post_review_comment`

## Quality & CI
- pytest + moto (AWS mocking)
- ruff (linter) + black (formatter)
- Pre-commit hooks encouraged

## Key Libraries
- `boto3` — AWS SDK
- `mcp` — MCP Python SDK
- `PyGithub` or raw `httpx` — GitHub API calls
- `pydantic` — data validation / structured outputs

## Conventions
- Type hints everywhere.
- One module per concern in `src/code_review_agent/`.
- Environment variables for config; secrets from Secrets Manager at runtime.
- Idempotent Lambda handler (safe to retry).
