# Product — Code Review Agent

## Vision
An automated code reviewer that analyzes GitHub pull requests and posts actionable, inline comments — reducing review latency and catching issues before human reviewers engage.

## Core Value Proposition
- **Speed**: Reviews posted within seconds of PR open/update.
- **Consistency**: Every PR gets the same baseline checks (style, security, complexity).
- **Cost-efficiency**: Runs on Bedrock Haiku to stay within free-tier credits.

## Target Users
- Solo developers and small teams using GitHub who want faster feedback loops.

## Key Workflows
1. GitHub sends PR webhook → API Gateway → Lambda.
2. Lambda fetches diff via MCP `read_pr_diff` tool.
3. Diff sent to Bedrock Claude Haiku for analysis.
4. Structured findings posted back via MCP `post_review_comment` tool.

## Non-Goals (v1)
- Full repo-level architectural analysis.
- Multi-model routing (Sonnet fallback).
- Self-hosted GitHub Enterprise support.
