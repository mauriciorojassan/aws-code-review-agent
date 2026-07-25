# Smoke Test Runbook

Manual verification steps for the Lambda handler orchestration. This runbook covers
what is exercised by **T4.4** in `.kiro/specs/cra-001/tasks.md`: prove the packaged
handler executes end-to-end against a realistic API Gateway event.

## Prerequisites

- **Python 3.12** on `PATH` (Lambda runtime target). Install via `mise`:
  ```bash
  mise install python@3.12
  mise use python@3.12          # writes/updates mise.toml
  ```
- **AWS SAM CLI** ≥ 1.100 (this repo tested with 1.164).
- **Docker daemon** running (for `sam local invoke` only).

## What each step verifies

| Step | Verifies | Requires Docker? |
|------|----------|------------------|
| 1. Direct-Python smoke   | Handler runs end-to-end on your host Python | no |
| 2. `sam validate --lint` | Template is well-formed CloudFormation + SAM   | no |
| 3. `sam build`           | Deployment package builds cleanly              | no |
| 4. `sam local invoke`    | Full runtime: API Gateway event + Lambda RIE   | yes |

If Docker misbehaves (see the caveat below), steps 1–3 cover the same
functional surface as step 4 with zero Docker dependency.

## Step 1 — Direct-Python invocation (fastest, no Docker)

```bash
export WEBHOOK_SECRET="local-dev-webhook-secret"
export GITHUB_TOKEN="not-a-real-token-just-for-local-testing"
export DIFF_CACHE_BUCKET="local-dev-cache-bucket"
export BEDROCK_MODEL_ID="anthropic.claude-3-haiku-20240307"

.venv/bin/python -c "
import json
from code_review_agent.handler import lambda_handler
with open('events/pr_closed_filtered.json') as f:
    event = json.load(f)
print(json.dumps(lambda_handler(event, None), indent=2))
"
```

**Expected output** (< 2 s):

```json
{
  "statusCode": 200,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"message\": \"ignored: ignored_action\"}"
}
```

`events/pr_closed_filtered.json` carries `action: "closed"`, which the action
filter rejects — the handler returns 200 immediately, with no diff-fetch,
Bedrock, or S3 traffic. This is the fastest way to sanity-check that:

- The handler imports and initializes without error.
- The signature validator accepts our fixture HMAC.
- The event filter chain routes correctly.

To exercise the full pipeline (with external calls that will fail against real
GitHub / Bedrock / S3), swap in `events/pr_opened.json`. This is only useful
when running against a real (or heavily mocked) cloud environment.

## Step 2 — Template validation (no Docker)

```bash
sam validate --lint
```

**Expected:** `template.yaml is a valid SAM Template` with no lint findings.

## Step 3 — Build the deployment package (no Docker)

```bash
sam build
```

**Expected:** `Build Succeeded` and a populated `.aws-sam/build/ReviewFunction/`
directory containing our source + all runtime dependencies (`boto3`, `pydantic`,
`httpx`, `botocore`, `s3transfer`, etc.). This confirms `src/requirements.txt`
lists everything the handler needs at runtime.

## Step 4 — `sam local invoke` (Docker required)

```bash
# Filtered event — proves the wire-up in < 3 s if Docker cooperates.
sam local invoke ReviewFunction \
  --event events/pr_closed_filtered.json \
  --env-vars events/env.json
```

**Expected:** JSON response `{"statusCode": 200, ...}` printed to stdout.

**Full-pipeline event (attempts real GitHub API — will 401 with the fake token):**

```bash
sam local invoke ReviewFunction \
  --event events/pr_opened.json \
  --env-vars events/env.json
```

### Known caveat: SAM CLI / Docker API drift on this dev host

On the machine used to author this runbook (Docker 29.6.2 + SAM CLI 1.164.0),
`sam local invoke` fails to complete because SAM's embedded Docker client
requests API version 1.35, receives a 400 "Minimum supported API version is
1.40" back from the daemon, falls back to 1.44, and then leaves the runtime
container in a state where `Container was not created. Skipping deletion` — so
the handler never actually runs inside the container.

This is **not** a code issue — the same handler runs correctly via Step 1
(direct-Python) and the SAM template packages correctly via Step 3. If Step 4
fails with the same symptom on your host, either:

- Upgrade the local Docker package, or
- Downgrade SAM CLI to a release that requests a compatible Docker API version,
  or
- Rely on Steps 1–3 as the smoke-test gate (recommended for this environment).

Deployment via `sam deploy --guided` is unaffected — that path does not use
Docker.

### Architecture note

`template.yaml` sets `Architectures: [arm64]` for cost-optimized Lambda
execution in production. `sam local invoke` on an x86_64 dev host needs either
`qemu-user-static` installed OR a temporary flip of the template to
`Architectures: [x86_64]` for the local run. **Do not commit the flip.**

## Fixture files

- `events/pr_opened.json` — API Gateway HTTP API v2 event carrying an
  `action: "opened"` payload with a valid HMAC-SHA256 signature computed with
  `local-dev-webhook-secret`. Exercises the full pipeline (diff fetch → Bedrock
  → publish); requires either real credentials or a mocked backend to succeed.
- `events/pr_closed_filtered.json` — same shape, `action: "closed"`. Exits at
  the action-filter step with HTTP 200 and no external I/O. Fastest wire-up
  check.
- `events/env.json` — env-var overrides passed via `--env-vars`. Sets the four
  variables the handler reads at cold start: `WEBHOOK_SECRET`, `GITHUB_TOKEN`,
  `DIFF_CACHE_BUCKET`, `BEDROCK_MODEL_ID`. `SECRETS_ARN` is intentionally
  unset — the handler's env-first credential resolution then uses
  `WEBHOOK_SECRET` and `GITHUB_TOKEN` directly, bypassing Secrets Manager.

## Regenerating fixtures

Both event files embed an HMAC-SHA256 signature computed over the exact bytes
of `body`. If you change the payload, regenerate:

```bash
.venv/bin/python -c "
import hashlib, hmac, json
payload = {...}    # your new payload
secret = b'local-dev-webhook-secret'
body_str = json.dumps(payload)
digest = hmac.new(secret, body_str.encode(), hashlib.sha256).hexdigest()
event = {
    'version': '2.0',
    'headers': {
        'x-github-event': 'pull_request',
        'x-hub-signature-256': f'sha256={digest}',
    },
    'requestContext': {'http': {'method': 'POST', 'path': '/webhook'}},
    'body': body_str,
    'isBase64Encoded': False,
}
print(json.dumps(event, indent=2))
"
```
