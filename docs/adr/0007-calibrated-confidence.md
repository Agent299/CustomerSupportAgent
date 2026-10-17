# ADR 0007: Calibrated 0–1 confidence score

- Status: Accepted
- Date: 2026-10-17

## Context

The chapter's `confidence_score: 7.2` has no defined computation, yet escalation depends on it.

## Decision

confidence = w1·retrieval + w2·citation_validity + w3·model_self_report, with weights and thresholds fitted on the golden set to maximize escalation recall.

## Consequences

Escalation behaviour is measurable and tunable; needs recalibration when models or retrieval change.
