# ADR 0004: Embed chunk text with breadcrumb and entities

- Status: Accepted
- Date: 2026-10-17

## Context

The chapter embeds only extracted entities and metadata, not the chunk text.

## Decision

Embedding input = `breadcrumb + chunk text + entity list`.

## Consequences

Better semantic recall; slightly more embedding tokens (cheap with text-embedding-3-small).
