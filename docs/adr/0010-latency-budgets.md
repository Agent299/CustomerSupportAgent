# ADR 0010: Explicit latency budgets

- Status: Accepted
- Date: 2026-10-17

## Context

The chapter states 3 s first reply, 60 s resolution, and a 5 s P90 alert.

## Decision

Ack < 300 ms; first token < 3 s P90; full answer < 10 s P90; escalation decision < 60 s; retrieval < 1.2 s (warm instance).

## Consequences

Every pipeline stage gets a time budget; cold starts on free hosting are reported separately.
