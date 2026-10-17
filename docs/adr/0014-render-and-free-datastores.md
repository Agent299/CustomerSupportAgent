# ADR 0014: Render for compute; free managed services for state

- Status: **Superseded** by [ADR 0016](0016-oracle-cloud-single-vm.md) (2026-10-17)
- Date: 2026-10-17

## Context

The user chose Render. Render's free tier covers web services, static sites and an in-memory Key Value; free Postgres expires after 30 days; workers and cron are not free.

## Decision

API = Render free web service (Docker); UI = Render Static Site; cache = Render Key Value; Postgres = Neon Free; OpenSearch = Aiven Free; graph = Neo4j AuraDB Free; ingestion = GitHub Actions with a Redpanda service container; snapshots = Cloudflare R2.

## Consequences

$0 hosting with cold starts (~1 min after 15 min idle) and small quotas; all endpoints configured via env vars so any store can be swapped.
