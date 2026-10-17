# ADR 0012: Version-aware, content-hash-deduplicated chunks

- Status: Accepted
- Date: 2026-10-17

## Context

We index several MongoDB versions; most pages are identical across versions; free-tier quotas are small.

## Decision

`chunk_id = sha256(normalized text)[:16]`; one chunk row with a `versions` list; `chunk_occurrences` maps chunks to (doc, version). All searches filter by version.

## Consequences

Storage stays close to single-version size; ingestion must maintain occurrence lists and garbage-collect orphans.
