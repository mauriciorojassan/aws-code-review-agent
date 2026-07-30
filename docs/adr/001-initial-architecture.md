# ADR 001 — Serverless GitHub Webhook Consumer on AWS SAM

## Status

Accepted

## Context

The system needed to review pull requests automatically without maintaining long-running infrastructure. The review bot had to respond within seconds of a PR event, scale to zero between events, and keep GitHub credentials and model invocations isolated.

## Decision

Use AWS SAM with API Gateway HTTP API v2 → AWS Lambda → Amazon Bedrock. The function is triggered only by GitHub webhooks, processes the diff synchronously, and posts comments via the GitHub REST API.

## Consequences

### Positive

- No persistent servers; cost scales with PR volume.
- Bedrock provides data residency and avoids direct model provider credentials.
- S3 caching reduces duplicate model invocations and keeps monthly cost under a few dollars at typical volume.

### Negative

- Cold start can add latency to the first review of an idle period.
- AWS credential rotation and SAM deployments are operational concerns for a self-hosted user.

## Alternatives Considered

- **GitHub Actions-based reviewer**: simpler for users, but harder to keep secrets in AWS and would couple execution to GitHub's runner environment.
- **Container service on ECS/Fargate**: easier to optimize latency, but adds always-on cost and operational surface for a side project.
