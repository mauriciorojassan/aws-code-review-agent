# Deployment Runbook

Step-by-step guide for deploying the Code Review Agent to AWS with SAM.

Covers two auth paths in parallel — pick one:

- **[Path A: Personal Access Token](#path-a--personal-access-token-pat)** —
  simplest setup, ~5 minutes. Best for a single repo, a solo maintainer, or a
  first-time deploy where you want to see the pipeline working end-to-end.
- **[Path B: GitHub App](#path-b--github-app)** — organization-scoped
  installation, per-repo permission granularity, better audit trail. Best for
  a team or enterprise deployment. ⚠️ **Requires an hourly token refresh
  handled outside this stack**: the current handler consumes a pre-generated
  installation access token from Secrets Manager and does not perform the
  JWT exchange itself; installation tokens expire in 1 hour. See
  [Future — auto-refresh in Lambda](#future--auto-refresh-in-lambda) for the
  planned in-stack solution.

Both paths use the same SAM stack; only the token you paste into Secrets
Manager differs.

---

## Prerequisites

- AWS account with credentials configured for the target region.
  - Verify: `aws sts get-caller-identity`
- **AWS SAM CLI** ≥ 1.100 (`sam --version`).
- **Python 3.12** on `PATH` — the Lambda runtime target. Install via
  [mise](https://mise.jdx.dev/) if your host runs a different version:
  ```bash
  mise install python@3.12
  mise use python@3.12
  ```
- **Docker** — required only if you want to run [smoke-test.md Step 4](./smoke-test.md#step-4--sam-local-invoke-docker-required)
  before deploying. The deploy itself does not use Docker.
- **Bedrock model access** in the target region for Claude Haiku 4.5 (the new
  default `us.anthropic.claude-haiku-4-5-20251001-v1:0` runs via the geographic
  cross-region inference profile on most accounts — enable "Claude Haiku 4.5" in
  the console). Enable in the AWS console: Bedrock → Model access → Manage model
  access → check "Claude Haiku 4.5" → Save. This can take a few minutes to
  activate. The Haiku 3.x family remains accepted too, so older accounts that
  still have Claude 3 Haiku granted explicitly keep working.

## IAM permissions the deployer needs

CloudFormation invokes the caller's credentials when creating resources on
your behalf, so the deployer's principal needs permission for **every**
service the template touches — not just CloudFormation and IAM.

**Simplest bootstrap** — attach the AWS-managed `AdministratorAccess`
policy to the deploying principal. This is the fastest path for a
first-time deploy or a solo maintainer working in a personal account.
Reduce to least-privilege once you have a working deploy to baseline
against.

**Least-privilege (production) bootstrap** — combine the following
service permissions (either as inline policies or a custom managed
policy):

- `cloudformation:*` — create/update/delete the stack itself.
- `iam:*` (or, tighter, `iam:CreateRole`, `iam:PutRolePolicy`,
  `iam:PassRole`, `iam:DeleteRole`, `iam:GetRole`) — the Lambda
  execution role.
- `lambda:*` — the review function.
- `apigateway:*` — the HTTP API + stage + integration.
- `s3:*` (or scoped to the diff-cache bucket ARN pattern) — the cache
  bucket + lifecycle rule.
- `secretsmanager:*` (or scoped to the secret ARN) — the GitHub
  credentials store.
- `cloudwatch:*` + `logs:*` — the Bedrock-invocation alarm and the
  Lambda log group.

The `bedrock:InvokeModel` permission on the specific Haiku model ARN is
attached to the *function's* execution role by the template — the
deployer does not need it directly.

---

## Step 1 — Verify locally

Before touching AWS, run the local gate to make sure the code is
deployable:

```bash
pytest --cov=code_review_agent --cov-fail-under=98
ruff check src/ tests/ mcp_server/
black --check src/ tests/ mcp_server/
sam validate --lint
sam build
```

All five must be clean. `sam build` produces `.aws-sam/build/` — that
artifact is what `sam deploy` uploads.

## Step 2 — First-time deploy with `sam deploy --guided`

```bash
sam deploy --guided
```

You will be prompted for the following. Suggested answers:

| Prompt | Suggested value | Notes |
|--------|-----------------|-------|
| Stack Name | `code-review-agent-dev` | Any name; `dev` and `prod` stacks can coexist. |
| AWS Region | `us-east-1` (or your target) | Must have Bedrock Haiku access enabled. |
| Parameter `Stage` | `dev` | Only `dev` or `prod` accepted by the template. |
| Confirm changes before deploy | `y` | Review the changeset the first time. |
| Allow SAM to create IAM roles | `y` | Required — Lambda execution role. |
| Disable rollback | `n` | Keep automatic rollback on failure. |
| Save arguments to samconfig.toml | `y` | Subsequent deploys can use `sam deploy` with no flags. |

The initial deploy takes ~2 minutes. Watch for `CREATE_COMPLETE` on all
resources.

### Capture the webhook URL

At the end, SAM prints the stack Outputs. Save `WebhookUrl` — you'll paste it
into GitHub in a later step:

```bash
aws cloudformation describe-stacks \
  --stack-name code-review-agent-dev \
  --query 'Stacks[0].Outputs[?OutputKey==`WebhookUrl`].OutputValue' \
  --output text
```

Expected format:
`https://xxxxxxxxxx.execute-api.<region>.amazonaws.com/dev/webhook`

### Note the Secrets Manager ARN

The template creates an empty secret named `code-review-agent/github-dev`.
Get its ARN:

```bash
aws secretsmanager describe-secret \
  --secret-id code-review-agent/github-dev \
  --query 'ARN' --output text
```

You will populate this secret in one of the two auth paths below.

---

## Path A — Personal Access Token (PAT)

Simplest setup. Direct match to the current handler: no token refresh needed
beyond your own rotation cadence.

### A1. Create a fine-grained PAT

1. GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token.
2. Token name: `code-review-agent-<stage>`.
3. Expiration: **90 days** (rotate before expiry — set a calendar reminder).
4. Repository access: choose the specific repositories the agent should review, or "All repositories" for org-wide coverage.
5. Repository permissions:
   - **Pull requests**: Read and write — the reviewer fetches the PR's diff
     and posts a review with inline comments.
   - **Contents**: Read-only — required for the `GET /repos/{owner}/{repo}/pulls/{pr}`
     endpoint (called with `Accept: application/vnd.github.v3.diff`) to
     return the diff payload when the head is on a private repo.
   - **Metadata**: Read-only (auto-added by GitHub when any repo permission
     is selected).
6. Generate token. **Copy the value immediately — GitHub only shows it once.**

### A2. Choose a webhook secret

Generate a strong random secret and save it — you'll need it both for Secrets
Manager and for GitHub's webhook config:

```bash
openssl rand -hex 32
```

### A3. Populate Secrets Manager

Store both values in the secret that the SAM stack created:

```bash
aws secretsmanager put-secret-value \
  --secret-id code-review-agent/github-dev \
  --secret-string '{
    "webhook_secret": "PASTE_YOUR_HEX_STRING_HERE",
    "github_token":   "ghp_PASTE_YOUR_PAT_HERE"
  }'
```

The handler reads these keys via `credentials.get_webhook_secret()` and
`credentials.get_github_token()` (see `src/code_review_agent/credentials.py`).

### A4. Configure the GitHub webhook

1. On the target repository (or org): Settings → Webhooks → Add webhook.
2. **Payload URL**: the `WebhookUrl` from Step 2.
3. **Content type**: `application/json`.
4. **Secret**: the same hex string from A2.
5. **SSL verification**: Enable.
6. Events: choose "Let me select individual events" → check only
   **Pull requests**.
7. Active: checked.
8. Add webhook.

GitHub sends a ping event; check the webhook's "Recent Deliveries" tab for a
`204` or `200` response.

Skip to [Step 3 — Post-deploy smoke check](#step-3--post-deploy-smoke-check).

---

## Path B — GitHub App

Organization-scoped installation, per-repo permission granularity, better
audit trail. **Current handler consumes a pre-generated installation access
token from Secrets Manager**; it does not perform the JWT exchange itself.
That means:

- **Installation tokens expire in 1 hour.**
- **Someone (you or an automated job outside this stack) must refresh the
  token before it expires.** Options:
  - Manual refresh + `put-secret-value` on a cron.
  - A separate scheduled Lambda that does the JWT → installation-token
    exchange and writes the result to this secret.
  - GitHub's official `actions/create-github-app-token` in a scheduled
    workflow that publishes to Secrets Manager.

See [Future — auto-refresh in Lambda](#future--auto-refresh-in-lambda) for the
planned in-stack solution.

### B1. Create the GitHub App

1. GitHub → Settings → Developer settings → GitHub Apps → New GitHub App.
2. Name: `Code Review Agent (<stage>)`.
3. Homepage URL: any placeholder (e.g., your repo URL).
4. Webhook: **enable**.
   - Webhook URL: the `WebhookUrl` from Step 2.
   - Webhook secret: generate with `openssl rand -hex 32` and save it.
5. Repository permissions:
   - **Pull requests**: Read and write — post the review + inline comments.
   - **Contents**: Read-only — read the diff payload from the pulls endpoint.
   - **Metadata**: Read-only (auto-added by GitHub when any repo permission
     is selected).
6. Subscribe to events: **Pull request**.
7. Where can this app be installed: Any account (or restrict to your org).
8. Create GitHub App.

### B2. Generate a private key

On the App's settings page → "Private keys" → Generate a private key. This
downloads a `.pem` file. Save it securely — it's how you'll mint installation
tokens.

Also record from the App settings page:

- **App ID** (numeric, top of the page).

### B3. Install the App

On the App's page → "Install App" → choose your org / account → select the
repositories to review → Install.

After installation, find the **Installation ID**:

```bash
# Requires that you already have a short-lived JWT signed with the App's
# private key; see GitHub's docs on generating one.
curl -H "Authorization: Bearer <JWT>" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/app/installations
```

The `id` field of the entry matching your org is the installation ID.

### B4. Mint an installation access token

Using the App ID, private key, and installation ID, generate an installation
token. GitHub's docs walk through this; short version with `gh` CLI + a
helper script:

```bash
# Using the official action locally: https://github.com/actions/create-github-app-token
# Or a small Python helper — see appendix at bottom of this doc.
INSTALLATION_TOKEN=$(python scripts/gen_installation_token.py \
  --app-id "$APP_ID" \
  --private-key-path ./private-key.pem \
  --installation-id "$INSTALLATION_ID")
```

**This token expires in 1 hour.** Before then, you (or your refresh
automation) must repeat this step and update the secret.

### B5. Populate Secrets Manager

```bash
aws secretsmanager put-secret-value \
  --secret-id code-review-agent/github-dev \
  --secret-string "{
    \"webhook_secret\": \"$WEBHOOK_SECRET_FROM_B1\",
    \"github_token\":   \"$INSTALLATION_TOKEN\"
  }"
```

The handler treats an installation token identically to a PAT — same
Secrets Manager key (`github_token`), same request headers. The only
difference is the lifecycle you (or your automation) manage outside the
stack.

Continue to [Step 3 — Post-deploy smoke check](#step-3--post-deploy-smoke-check).

---

## Step 3 — Post-deploy smoke check

Trigger a real event by opening (or updating) a PR against the configured
repo. Then confirm the pipeline ran:

```bash
# CloudWatch Logs — recent Lambda invocations
aws logs tail /aws/lambda/code-review-agent-dev --since 5m --follow
```

Look for one JSON-formatted log line per PR event with `status: "success"`
(or `status: "skipped"` if the PR was filtered).

You can also invoke the smoke-test fixture directly against the deployed
function (bypasses GitHub but exercises the deployed IAM + Bedrock + S3
wiring):

```bash
aws lambda invoke \
  --function-name code-review-agent-dev \
  --payload fileb://events/pr_closed_filtered.json \
  /tmp/response.json
cat /tmp/response.json
```

Expected: `{"statusCode": 200, ..., "body": "{\"message\": \"ignored: ignored_action\"}"}`.

For a full local invocation before opening a real PR, see
[docs/smoke-test.md](./smoke-test.md).

---

## Subsequent deploys

Once `samconfig.toml` is saved, redeployment is one command:

```bash
sam build && sam deploy
```

CI runs `sam validate --lint` on every push (see
`.github/workflows/ci.yml`) but does not auto-deploy — deploys stay
manual for v1.

## Rollback

If a deploy goes bad:

```bash
# Rollback to the previous stack version (CloudFormation handles this
# automatically on failed CREATE/UPDATE, but for a "revert to the last
# known good" after a bad manual change:)
git checkout <previous-good-sha>
sam build && sam deploy --no-confirm-changeset
```

To tear the whole stack down (destroys the S3 bucket **and its contents**,
the Secrets Manager secret, and the Lambda + API Gateway):

```bash
aws cloudformation delete-stack --stack-name code-review-agent-dev
aws cloudformation wait stack-delete-complete --stack-name code-review-agent-dev
```

The Secrets Manager secret enters a 7-day recovery window by default. To
delete immediately (destructive):

```bash
aws secretsmanager delete-secret \
  --secret-id code-review-agent/github-dev \
  --force-delete-without-recovery
```

---

## Future — auto-refresh in Lambda

Currently the handler consumes whatever `github_token` sits in Secrets
Manager. For Path B (GitHub App), an operator or external job must refresh
the installation token every hour.

The planned improvement (scoped for a later wave) is to move the JWT →
installation-token exchange **inside** the Lambda:

- Store the App's private key PEM in Secrets Manager (under a new key,
  e.g., `github_app_private_key`).
- Store the App ID and installation ID as env vars on the function.
- On cold start (or on every invocation with a 5-minute cache), the handler
  signs a JWT, calls `POST /app/installations/{id}/access_tokens`, and uses
  the returned token for the current review.
- Falls back to `github_token` from Secrets Manager if the App credentials
  are not configured — preserving Path A compatibility.

Not implemented in v1.

---

## Environment variables (reference)

The Lambda reads these at call time (not at import), so `sam local invoke
--env-vars events/env.json` and Secrets Manager both work:

| Variable | Source | Purpose |
|----------|--------|---------|
| `WEBHOOK_SECRET` | env (dev) → Secrets Manager `webhook_secret` (prod) | HMAC key for signature validation. |
| `GITHUB_TOKEN` | env (dev) → Secrets Manager `github_token` (prod) | Bearer token for GitHub API calls. |
| `SECRETS_ARN` | template `!Ref GitHubSecrets` | Secrets Manager secret to read when env is absent. |
| `DIFF_CACHE_BUCKET` | template `!Ref DiffCacheBucket` | S3 bucket for diff + analysis cache. |
| `BEDROCK_MODEL_ID` | template default inference profile id | Default is `us.anthropic.claude-haiku-4-5-20251001-v1:0` (Haiku 4.5 geographic cross-region inference profile). Accepted family: `anthropic.claude-3-haiku*`, `anthropic.claude-3-5-haiku*`, `anthropic.claude-3-7-haiku*`, `anthropic.claude-haiku-4-5*`, `us.anthropic.claude-haiku-4-5*`, `global.anthropic.claude-haiku-4-5*` (Haiku 3.0 / 3.5 / 3.7 / 4.5 foundation, plus Haiku 4.5 geographic and global inference profile ids). Other values fail-closed at review time. |
| `METRICS_NAMESPACE` | (optional, defaults to `CodeReviewAgent`) | CloudWatch custom-metrics namespace. |

`WEBHOOK_SECRET` and `GITHUB_TOKEN` env vars **win** over Secrets Manager
when set — useful for local dev and for `sam local invoke`. Production
deployments leave both unset in the environment; the handler reads from
Secrets Manager only.

Note on the default `BEDROCK_MODEL_ID`: `us.anthropic.claude-haiku-4-5-20251001-v1:0`
is a geographic cross-region inference profile id — AWS auto-routes
`InvokeModel` calls across `us-east-1`, `us-east-2`, and `us-west-2` based on
regional capacity. The SAM template IAM policy grants `bedrock:InvokeModel` on
the inference profile ARN (resolved via `!Sub` with `${AWS::Region}` and
`${AWS::AccountId}`) plus the three regional foundation-model ARNs guarded by
the `bedrock:InferenceProfileArn` condition, matching the AWS official
Geographic cross-region inference policy pattern.
