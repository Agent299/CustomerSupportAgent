# ADR 0008: Human-approved resolved tickets feed the knowledge base

- Status: Accepted
- Date: 2026-10-17

## Context

There are no historical tickets; the chapter relies on 50k resolved tickets as a knowledge source.

## Decision

When an agent resolves an escalation and clicks approve, a `TICKET` source is ingested with `RESOLVES` / `HAS_ROOT_CAUSE` edges.

## Consequences

The KB grows from real usage; human approval keeps bad resolutions out.
