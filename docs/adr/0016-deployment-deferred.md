# ADR 0016: Run locally only; defer cloud deployment

- Status: Accepted
- Date: 2026-10-17
- Supersedes: [ADR 0014](0014-render-and-free-datastores.md)

## Context

Two hosting plans were explored:

1. **Render** plus free managed datastores (ADR 0014).
2. **One Oracle Cloud Always Free VM** running the whole stack. This was prototyped and tested locally on arm64 but never merged.

Setting up Oracle ran into practical problems. The owner prefers to keep building and to choose a cloud provider later.

## Decision

- The project runs **locally only**, on the Docker Compose stack in `infra/docker-compose.yml`. No deployment code, CD workflow or hosting-specific configuration lives in the repo for now.
- Code stays **host-agnostic**, so any target can be added later without restructuring:
  - every endpoint and secret comes from environment variables;
  - the backend image has `api` and `worker` targets;
  - the UI is a static export;
  - the API never talks to Kafka directly (outbox + `relay`).

## Consequences

- CI keeps checking that the images build, but nothing is deployed.
- Choosing a host later is a new ADR. Lessons from the explored options:
  - A single VM fits the measured footprint: about 2.8 GB RAM idle with all services.
  - Serving the UI and API from one origin avoids CORS and third-party-cookie issues for Phase 4 auth.
  - Free managed tiers bring cold starts and quotas.
