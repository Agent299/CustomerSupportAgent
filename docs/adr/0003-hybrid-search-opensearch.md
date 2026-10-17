# ADR 0003: Hybrid BM25 + kNN search in OpenSearch

- Status: Accepted
- Date: 2026-10-17

## Context

Support queries contain exact identifiers (`E11000`, `$lookup`, `maxPoolSize`) that embeddings match poorly; the chapter's own example is an error code.

## Decision

Use OpenSearch with a hybrid query (BM25 + k-NN) and a normalization search pipeline; weights tuned on the golden set.

## Consequences

One engine for both signals and metadata filters (version, security level). Weights become a tunable parameter that must be evaluated.
