# Code Review Agent 🤖

Automated GitHub PR reviewer powered by Amazon Bedrock (Claude Haiku).

## What it does

1. Receives GitHub PR webhooks (open/sync events)
2. Fetches the diff via MCP tools
3. Analyzes changes with Bedrock Claude Haiku
4. Posts structured inline comments back to the PR

## Architecture

```
GitHub → API Gateway HTTP → Lambda → Bedrock (Haiku)
                                  ↕
                              S3 (cache)
```

## Quick Start

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Lint & format
ruff check src/ tests/
black --check src/ tests/

# Local Lambda invoke
sam build && sam local invoke ReviewFunction --event events/pr_opened.json

# Deploy
sam deploy --guided
```

## Project Structure

See [.kiro/steering/structure.md](.kiro/steering/structure.md) for full layout.

## Cost Governance

This project is designed to stay under $3/month. See [aws-cost-governance.md](.kiro/steering/aws-cost-governance.md).

## MCP Server

The `mcp_server/` provides stdio-based tools:
- `read_pr_diff` — fetch PR diff from GitHub
- `post_review_comment` — post inline review comments

Run standalone: `python -m mcp_server.server`

## License

MIT
