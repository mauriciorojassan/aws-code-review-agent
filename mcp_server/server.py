"""MCP stdio server exposing GitHub PR tools.

Tools:
- read_pr_diff: Fetches the unified diff for a given PR.
- post_review_comment: Posts inline review comments to a PR.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logger = logging.getLogger(__name__)

server = Server("code-review-agent")


@server.list_tools()
async def list_tools() -> list[Tool]:
    """Return available MCP tools."""
    return [
        Tool(
            name="read_pr_diff",
            description="Fetch the unified diff for a GitHub pull request",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner"},
                    "repo": {"type": "string", "description": "Repository name"},
                    "pr_number": {"type": "integer", "description": "Pull request number"},
                },
                "required": ["owner", "repo", "pr_number"],
            },
        ),
        Tool(
            name="post_review_comment",
            description="Post inline review comments to a GitHub pull request",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner"},
                    "repo": {"type": "string", "description": "Repository name"},
                    "pr_number": {"type": "integer", "description": "Pull request number"},
                    "commit_id": {"type": "string", "description": "Head commit SHA"},
                    "findings": {
                        "type": "array",
                        "description": "List of review findings",
                        "items": {
                            "type": "object",
                            "properties": {
                                "file": {"type": "string"},
                                "line": {"type": "integer"},
                                "severity": {"type": "string"},
                                "message": {"type": "string"},
                                "suggestion": {"type": "string"},
                            },
                        },
                    },
                },
                "required": ["owner", "repo", "pr_number", "commit_id", "findings"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Dispatch tool calls."""
    if name == "read_pr_diff":
        return await _read_pr_diff(**arguments)
    elif name == "post_review_comment":
        return await _post_review_comment(**arguments)
    else:
        raise ValueError(f"Unknown tool: {name}")


async def _read_pr_diff(owner: str, repo: str, pr_number: int) -> list[TextContent]:
    """Fetch PR diff from GitHub API."""
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
    headers = {
        "Accept": "application/vnd.github.v3.diff",
        # TODO: Add Authorization header from Secrets Manager
    }

    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()

    return [TextContent(type="text", text=response.text)]


async def _post_review_comment(
    owner: str,
    repo: str,
    pr_number: int,
    commit_id: str,
    findings: list[dict[str, Any]],
) -> list[TextContent]:
    """Post review comments to GitHub PR."""
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/reviews"

    comments = [
        {
            "path": f["file"],
            "line": f["line"],
            "body": f"**[{f['severity'].upper()}]** {f['message']}"
            + (f"\n\n💡 Suggestion: {f['suggestion']}" if f.get("suggestion") else ""),
        }
        for f in findings
    ]

    body = {
        "commit_id": commit_id,
        "event": "COMMENT",
        "comments": comments,
        "body": _summary_body(findings),
    }

    headers = {
        "Accept": "application/vnd.github.v3+json",
        # TODO: Add Authorization header from Secrets Manager
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=body, headers=headers)
        response.raise_for_status()

    return [TextContent(type="text", text=f"Review posted with {len(comments)} comments")]


def _summary_body(findings: list[dict[str, Any]]) -> str:
    """Generate summary text for the review."""
    errors = sum(1 for f in findings if f.get("severity") == "error")
    warnings = sum(1 for f in findings if f.get("severity") == "warning")
    infos = sum(1 for f in findings if f.get("severity") == "info")
    return (
        f"## 🤖 Code Review Agent\n\n"
        f"| Severity | Count |\n|----------|-------|\n"
        f"| 🔴 Error | {errors} |\n"
        f"| 🟡 Warning | {warnings} |\n"
        f"| 🔵 Info | {infos} |\n"
    )


async def main() -> None:
    """Run the MCP server via stdio."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
