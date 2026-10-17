# ADR 0015: Ingest MongoDB's official Markdown pages, not HTML

- Status: Accepted
- Date: 2026-10-17

## Context

The plan assumed crawling rendered HTML pages and parsing them with `trafilatura`/`selectolax`. A source investigation (Phase 1) found:

- Every docs page has an official Markdown twin at `<page>.md`. Example: `/docs/manual/core/index-unique.md` is about 11 KB, while the HTML version is about 858 KB. MongoDB's `/docs/llms.txt` explicitly tells agents to use the `.md` pages.
- The server manual is versioned by path: `manual` (= 9.0, current), `v8.3`, `v8.0`, `v7.0`. Each has a sitemap at `/docs/<path>/sitemap-0.xml`. Page counts are 2,323 / 2,274 / 2,052 / 1,743.
- The sitemaps have no `<lastmod>`, and the `.md` responses have no ETag.
- robots.txt allows these paths.

## Decision

- Ingest `<page>.md` for versions **9.0, 8.0 and 7.0**. These are the major releases self-hosted users run; 8.3 is a rapid release. The list is configurable in `shared/sources.py`.
- Chunk deterministically on Markdown headings (`workers/ingest/chunker.py`). No HTML parser dependency.
- Detect changes with a SHA-256 of the redacted Markdown.
- Normalize version-specific links to a `{docs_version}` placeholder, so identical sections across versions hash to the same chunk ID (ADR 0012).

## Consequences

- Simpler, faster and more faithful parsing: about 80× less data transferred. Code fences and heading levels come out exact.
- Each run re-downloads every page to compare hashes: about 6.1k requests, roughly 50 min at 2 req/s, about 70 MB. That's acceptable for a nightly job.
- We depend on MongoDB continuing to publish `.md` pages. If they stop, the chunker can be fed HTML converted to Markdown without changing anything downstream.
