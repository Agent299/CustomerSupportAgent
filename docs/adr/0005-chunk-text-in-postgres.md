# ADR 0005: Chunk text in Postgres; object storage for raw snapshots only

- Status: Accepted
- Date: 2026-10-17

## Context

The chapter stores raw chunks in S3 and fetches them per request after retrieval.

## Decision

Store chunk text in Postgres (`chunks`); store raw HTML page snapshots in S3-compatible storage (R2).

## Consequences

Top-K fetch is one SQL query. Snapshots allow re-chunking without re-crawling. Postgres size must be watched against the Neon free quota.
