# Structure — Code Review Agent

```
code-review-agent/
├── .kiro/
│   ├── steering/          # Product, tech, structure, governance docs
│   ├── hooks/             # Kiro automation hooks
│   ├── agents/            # Agent definitions
│   └── specs/cra-001/     # Feature spec (requirements → design → tasks)
├── src/
│   └── code_review_agent/
│       ├── __init__.py
│       ├── handler.py     # Lambda entry point
│       ├── reviewer.py    # Bedrock interaction logic
│       ├── diff_cache.py  # S3 caching layer
│       └── models.py      # Pydantic models (webhook, findings)
├── mcp_server/
│   ├── __init__.py
│   └── server.py          # MCP stdio server (read_pr_diff, post_review_comment)
├── tests/
│   ├── conftest.py
│   ├── test_handler.py
│   └── test_reviewer.py
├── template.yaml           # SAM template
├── pyproject.toml          # Project metadata, ruff/black config
├── requirements.txt        # Lambda runtime deps
├── README.md
└── .gitignore
```

## Module Responsibilities
| Module | Role |
|--------|------|
| `handler.py` | Receives webhook, validates signature, orchestrates review |
| `reviewer.py` | Builds prompt, calls Bedrock, parses structured response |
| `diff_cache.py` | Stores/retrieves diffs in S3 to avoid duplicate analysis |
| `models.py` | Pydantic schemas for webhook payload, review findings |
| `mcp_server/server.py` | Exposes MCP tools for diff fetching and comment posting |
