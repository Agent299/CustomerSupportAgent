# ADR 0001: Python workers instead of Spark/Glue; keep topic contracts

- Status: Accepted
- Date: 2026-10-17

## Context

The chapter uses Kafka + Spark (scaled via CloudWatch/Glue) + Step Functions. Our load is ~6k pages per version and ~100 changes/day.

## Decision

Keep Kafka-API topics (`raw_documents`, `chunked_documents`, `ticket_events`, `*.dlq`) on Redpanda; implement crawl/chunk and EEEP as Python consumers run as batch jobs.

## Consequences

No JVM cluster to operate. Spark can replace a worker later without changing producers or consumers downstream, because the event schemas are the contract.
