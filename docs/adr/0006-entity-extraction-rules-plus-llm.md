# ADR 0006: Entity extraction via rules plus the cheapest capable LLM

- Status: Accepted
- Date: 2026-10-17

## Context

The chapter assumes an internally hosted fine-tuned extraction model; we have no training data.

## Decision

Deterministic rules for structural and identifier edges; the cheapest model that passes the extraction eval for semantic relations, via structured outputs and the Batch API.

## Consequences

Cheap and auditable (`extracted_by` on every edge). Quality bounded by a fixed ontology; extend the ontology deliberately.
