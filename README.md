# MongoDB Support Agent (GraphRAG)

A customer support agent for MongoDB developers. It answers questions from the official docs using **hybrid search** (BM25 and vectors in OpenSearch) plus a **knowledge graph** (Neo4j), cites its sources, and hands off to a human agent console when it isn't confident.

The design is based on Chapter 6 of *Systems Design for the LLM Era*. This is a non-commercial portfolio project.

- Full plan, design and rationale: [`docs/PLAN.md`](docs/PLAN.md)
- Decisions: [`docs/adr/`](docs/adr/)

## Stack

FastAPI (Python 3.13, uv) · Next.js (static export) · OpenAI · OpenSearch · Neo4j · PostgreSQL · Valkey · Redpanda · Render

## Quick start

Prerequisites: Docker, [uv](https://docs.astral.sh/uv/), Node 24 + pnpm.

```bash
cp .env.example .env      # add OPENAI_API_KEY
make install
make up                   # Postgres, Valkey, Redpanda, OpenSearch, Neo4j (waits until healthy)
make ingest-smoke         # docs → Redpanda → chunks (Postgres) → embeddings → OpenSearch, 20 pages/version
make api                  # http://localhost:8000/docs
make web                  # http://localhost:3000
make check                # lint + types + tests + web build (same as CI)
```

| Service | Local URL |
|---|---|
| API docs | http://localhost:8000/docs |
| OpenSearch | http://localhost:9200 |
| Neo4j Browser | http://localhost:7474 (neo4j / localdevpassword) |
| Kafka API (Redpanda) | localhost:19092 |

## Contributing workflow

`main` is protected: nobody can push to it directly. Every change goes through a branch and a pull request, and can only be merged once the **`CI passed`** check is green. That check covers backend lint, types and tests; the web lint and build; and the API Docker build.

```bash
git switch main && git pull
git switch -c feat/<short-name>      # or fix/, ci/, docs/, chore/
# ...work, then run the same checks CI runs:
make check
git push -u origin HEAD
gh pr create --fill                  # CI runs on the PR
gh pr merge --squash --delete-branch # allowed only after CI passes
```

No OpenAI key handy? `EMBEDDING_PROVIDER=hash make ingest-smoke` uses a deterministic offline embedder; switching back to OpenAI and running `make reindex` re-embeds everything.

## Status

Phase 0 (foundation) is complete. Phase 1 (ingestion) is in progress: docs → chunks → embeddings → hybrid search index. See [`docs/PLAN.md` §13](docs/PLAN.md#13-delivery-phases) for the roadmap.

---

Answers are derived from MongoDB documentation (CC BY-NC-SA). Not affiliated with MongoDB, Inc.
