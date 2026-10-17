# ADR 0002: Modular monolith for the core API

- Status: Accepted
- Date: 2026-10-17

## Context

The chapter splits chat service, RAG orchestrator and GenAI service into separate services. Each network hop costs latency against the 3 s first-token target, and free hosting gives one small instance.

## Decision

One FastAPI deployable with packages `auth`, `chat`, `orchestrator`, `retrieval`, `genai`, `tickets`, `privacy`, `ingest_api`. Packages talk through explicit interfaces, never through each other's internals. Workers are separate processes.

## Consequences

Lower latency and simpler ops. Splitting later is a deployment change, not a rewrite. Requires discipline about module boundaries (enforced in code review/lint).
