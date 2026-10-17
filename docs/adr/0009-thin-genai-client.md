# ADR 0009: Thin in-house GenAI client on the official OpenAI SDK

- Status: Accepted
- Date: 2026-10-17

## Context

The chapter's GenAI service needs retries, circuit breakers and fallback.

## Decision

Own small module on `openai` + `tenacity` + a custom latency-aware circuit breaker; task→model registry from config.

## Consequences

No heavy proxy dependency; another provider can be added behind the same interface later.
