# ADR 0011: Deterministic first; cheapest capable model; frontier only for complex tasks

- Status: Accepted
- Date: 2026-10-17

## Context

Self-funded portfolio project; cost must stay minimal without sacrificing answer quality.

## Decision

Use rules wherever practical. For LLM tasks, start at the cheapest tier and move up only if the task's eval fails; answer synthesis uses a frontier model. Model IDs are configuration.

## Consequences

Every LLM call needs a justification and a per-task eval set.
