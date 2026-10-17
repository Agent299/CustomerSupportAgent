# AI-Powered Customer Support Agent — Implementation Plan

> A GraphRAG support agent for MongoDB developers, based on Chapter 6 ("Case Study: AI-Powered Customer Support Agent") of *Systems Design for the LLM Era*. Built to production-MVP quality as a **non-commercial portfolio showcase**.

| | |
|---|---|
| Owner | Solo developer |
| Status | Phase 0 (foundation) |
| Last updated | 2026-10-17 |
| Hosting | Render (API + static UI + Key Value) with free managed datastores |

---

## Contents

1. [Summary](#1-summary)
2. [Scope](#2-scope)
3. [Analysis of the source design](#3-analysis-of-the-source-design)
4. [Key decisions and rationale](#4-key-decisions-and-rationale)
5. [LLM usage policy](#5-llm-usage-policy)
6. [High-level design](#6-high-level-design)
7. [Technology stack](#7-technology-stack)
8. [Low-level design](#8-low-level-design)
9. [Deployment and hosting](#9-deployment-and-hosting)
10. [Non-functional targets, monitoring and alerting](#10-non-functional-targets-monitoring-and-alerting)
11. [Evaluation strategy](#11-evaluation-strategy)
12. [Failure mode analysis](#12-failure-mode-analysis)
13. [Delivery phases](#13-delivery-phases)
14. [Risks and open items](#14-risks-and-open-items)

---

## 1. Summary

Developers ask questions about MongoDB in a web chat widget after they sign in with a code sent by email. The agent:

1. answers from the official MongoDB documentation, covering several server versions, with **cited sources**;
2. uses **hybrid retrieval**: BM25 + vector search in OpenSearch, expanded through a **knowledge graph** in Neo4j (GraphRAG);
3. **escalates** to a human agent console when it can't answer confidently, the question is out of scope, or the user asks for a human. It hands over a structured summary of the chat;
4. keeps its knowledge fresh through an **event-driven ingestion pipeline**, with Kafka-compatible topics (Redpanda);
5. is measured continuously against a **golden dataset**, with LLM-as-a-judge scoring and human review.

## 2. Scope

**In scope (MVP)**
- Knowledge source: public MongoDB docs for the **current release and the two previous major/minor releases**, with version filtering.
- Channel: an embeddable **web chat widget**.
- Customers sign in with **email verification** (passwordless one-time code).
- Escalation goes to a **custom agent console**: queue, AI summary, transcript, live takeover, resolution.
- Resolved tickets can be approved by an agent and fed back into the knowledge base.
- Evaluation suite, observability, PII masking, cost controls.

**Out of scope (MVP)**
- Slack and email channels, tool use / actions such as "restart cluster", community and GitHub sources, multiple languages, commercial use.

**Licensing note.** I believe MongoDB documentation is published under a Creative Commons non-commercial share-alike licence (to verify). This project is non-commercial. Every answer links to its source page, and the UI footer says: *"Answers derived from MongoDB documentation (CC BY-NC-SA). Not affiliated with MongoDB, Inc."*

## 3. Analysis of the source design

The chapter specifies:

- **Ingestion:** webhook → Kafka `raw_documents` → Spark chunking + PII redaction → S3 + `chunked_documents` → entity extraction and embedding processor (EEEP) → OpenSearch (vectors) and Neptune (graph). Spark scales on topic lag, with a nightly 02:00 run.
- **Query path:** chat service with session history in Redis → RAG orchestrator. The orchestrator checks the FAQ cache, rewrites the query using chat history, runs vector search, traverses the graph, fetches chunks from S3, synthesizes an answer, and scores its confidence.
- **Cross-cutting:** a GenAI service with circuit breakers and provider fallback; response, prompt and coalescing caches; golden dataset, LLM-as-a-judge and human-in-the-loop evaluation; monitoring thresholds; failure mode analysis; a ticket state machine in PostgreSQL.

Gaps and inconsistencies this plan resolves:

| # | Issue in the chapter | Resolution |
|---|---|---|
| G1 | The latency targets conflict: 3 s first reply, 60 s resolution, and a 5 s P90 alert | Explicit budgets; see §10 |
| G2 | `confidence_score: 7.2` has no defined scale or method, yet escalation depends on it | A calibrated 0–1 composite score; see §8.6 |
| G3 | Kafka + Spark + Glue + Step Functions for about 2k updates a day | Python workers, keeping the topic contracts (D1) |
| G4 | Only entities and metadata are embedded, not chunk text, which hurts recall | Embed the chunk text with its breadcrumb and entities prepended (D4) |
| G5 | Assumes a fine-tuned extraction model and 50k historical tickets | Rules plus the cheapest capable LLM; tickets accumulate from our own resolved escalations (D6, D8) |
| G6 | The ticket state machine and Postgres tables appear only in the recap | Fully specified in §8.4 and §8.7 |
| G7 | "UI only renders cited answers" conflicts with token streaming | A gate before generation plus validation after it; see §8.6 |

## 4. Key decisions and rationale

Each decision has an ADR in [`docs/adr/`](adr/).

| ID | Decision | Rationale |
|---|---|---|
| D1 | **Keep the event-driven topics and replace Spark/Glue with Python workers** | About 6k pages per version and roughly 100 changes a day don't justify a JVM cluster. Keeping the topic contracts means Spark could be swapped back in without touching anything downstream. |
| D2 | **A modular monolith:** one core API holds chat, orchestrator, genai, tickets and auth as packages; workers run as separate batch jobs | Every network hop eats into the 3 s budget. One deployable fits a solo developer and Render's free tier. The package boundaries let us split it later. |
| D3 | **Hybrid BM25 + kNN in OpenSearch** | Identifiers like `E11000` and `$lookup` need lexical matching. One engine does both in a single query. |
| D4 | **Embed `breadcrumb + chunk text + entities`** | Embedding only metadata throws away the wording semantic search relies on. The breadcrumb keeps the chapter's "smart chunking" benefit. |
| D5 | **Chunk text lives in Postgres; object storage holds only raw page snapshots** | Fetching the top-K chunks is one SQL round trip instead of K S3 GETs. Snapshots allow re-chunking without re-crawling. |
| D6 | **Entity extraction = deterministic rules + the cheapest capable LLM (Batch API)** | There is no fine-tuning data. Headings, links and identifier patterns give high-precision edges for free. |
| D7 | **A calibrated 0–1 confidence score** | Makes escalation measurable and tunable against the golden set. |
| D8 | **Human-approved resolved tickets are ingested back into the KB** | Builds up the chapter's "historical tickets" source over time. |
| D9 | **A thin GenAI client of our own on the official `openai` SDK** (retries, circuit breaker, model fallback) | Gives the chapter's resilience without a heavy proxy dependency. |
| D10 | **Explicit latency budgets** (§10) | Reconciles G1. |
| D11 | **Deterministic first; cheapest capable model for easy tasks; frontier model only for complex tasks** | Cost control. See §5. |
| D12 | **Version-aware, content-hash-deduplicated chunks** | Supports several versions without multiplying storage. This matters because the free-tier quotas are small. |
| D13 | **Passwordless email-code auth that we build ourselves** | It's small, and it's the same mechanism for customers and agents (an agent is a user with a role). |
| D14 | **Render for compute; free managed services for state** | Your hosting choice. Stateful services aren't free on Render (see §9). |

## 5. LLM usage policy

**Rule:** don't call a model if deterministic code can do the job. When a model is needed:
- **easy task** → the *cheapest* model that passes that task's eval;
- **complex task** → a high-end / frontier model.

"Correct" is defined by a small eval set for each task. Every task starts on the cheapest tier and moves up only when it fails. Model IDs live in configuration and are never hard-coded.

| Task | Implementation | Tier |
|---|---|---|
| PII masking | Regex + Presidio pattern recognizers (no spaCy NER model) | Deterministic |
| FAQ match, cache keys, chunking, structural edges, error-code and method extraction | Rules | Deterministic |
| Version / driver detection | Regex + a known-versions table | Deterministic |
| Out-of-scope detection (billing, refunds, account access) | Keyword and pattern rules first; LLM only when ambiguous | Deterministic, then easy |
| Frustration signal | Lexicon scorer (e.g. VADER) + repeat-turn rules | Deterministic |
| Confidence score, citation validation, routing, ticket transitions | Math and validation | Deterministic |
| Embeddings | `text-embedding-3-small`; move to `-large` only if retrieval evals require it | Cheapest embedding |
| Contextual query rewrite (multi-turn) | Skipped when turn 1 is self-contained | Easy (cheapest) |
| Entity / relation extraction at ingestion | Structured output, Batch API | Easy (cheapest) |
| Escalation summary | Structured output | Easy (cheapest) |
| **Answer synthesis** | Streamed, with citations | **Hard (frontier)** |
| Groundedness / quality judge | On a sample of live answers + the full golden set in CI | Mid–high (stronger than what it grades) |

Later optimization: if evals show a cheaper model passes on a category (e.g. FAQ-like questions with high retrieval confidence), a rule-based router sends that category to it.

## 6. High-level design

```
                ┌──────────────────── Next.js (static export) ────────────────────┐
                │  /widget  (customer chat, embeddable)    /console  (agents)      │
                └──────────────┬──────────────────────────────────┬────────────────┘
                               │ REST + SSE (JWT)                 │ REST + SSE (JWT, role=agent)
                ┌──────────────▼──────────────────────────────────▼────────────────┐
                │                       CORE API (FastAPI)                         │
                │ auth/        email code login, JWT, quotas, Turnstile            │
                │ chat/        sessions, SSE streaming, request coalescing         │
                │ privacy/     PII masking middleware                              │
                │ orchestrator/ FAQ → rewrite → hybrid search → graph expand →     │
                │              pack → synthesize → validate → score → route        │
                │ genai/       OpenAI client, prompts, breaker, fallback, cost     │
                │ tickets/     state machine, escalation, summary, console API     │
                │ ingest_api/  POST /api/ingest/collect → outbox                   │
                └───┬─────────┬───────────┬───────────┬────────────────────────────┘
                    │         │           │           │
              Key Value   Postgres    OpenSearch    Neo4j
              (sessions,  (users,     (BM25+kNN     (knowledge
               caches,     tickets,    kb_chunks)    graph)
               pub/sub,    messages,
               quotas)     chunks, outbox, evals)

  ─────────────────────── Ingestion (batch, event-driven) ───────────────────────
   trigger: nightly cron | manual dispatch | outbox rows
        │
        ▼
   ┌───────────┐ raw_documents ┌──────────────┐ chunked_documents ┌─────────────┐
   │ Scheduler │──────────────▶│ Crawl+Chunk  │──────────────────▶│ EEEP worker │
   │ sitemap   │   (Redpanda)  │ fetch, parse,│    (Redpanda)     │ rules + LLM │
   │ diff +    │               │ PII redact,  │                   │ entities,   │
   │ outbox    │               │ chunk, dedupe│                   │ embeddings  │
   └───────────┘               │ → Postgres,  │                   │ → OpenSearch│
                               │   R2 snapshot│                   │ → Neo4j     │
                               └──────────────┘                   └─────────────┘
   Observability: OpenTelemetry → Grafana Cloud (metrics/logs) · Langfuse Cloud (LLM traces, cost, scores)
```

**Two paths, as in the chapter**
- **Query path (synchronous):** widget → core API → caches and retrieval → OpenAI → answer streamed over SSE, or escalation to the console.
- **Ingestion path (asynchronous):** never runs inside the query path. Locally it uses Docker Compose. In production it runs as a scheduled GitHub Actions job with Redpanda as a service container (see §9).

## 7. Technology stack

| Layer | Choice | Why |
|---|---|---|
| Backend language | **Python 3.13**, managed with **uv** | Best RAG / graph / eval ecosystem; fast, reproducible environments |
| API | **FastAPI**, Pydantic v2, pydantic-settings, Uvicorn | Async I/O for the request path that fans out to many stores; schemas are the API contract |
| Persistence | SQLAlchemy 2 (async) + asyncpg, **Alembic** migrations | Mature, typed, works with Postgres anywhere |
| Orchestration | **Plain async pipeline** (explicit stages) | The tiered flow is fixed; easier to keep within latency budgets and to test. Revisit (e.g. LangGraph) when tool use arrives. |
| LLM | **OpenAI** (`openai` SDK), task-tiered per §5 | User choice; structured outputs; Batch API for ingestion |
| Event bus | **Redpanda** (Kafka API) via `aiokafka` | Single container, no ZooKeeper; same client code as Kafka/MSK |
| Search | **OpenSearch 3.x**: k-NN (HNSW, cosine) + BM25 + hybrid normalization processor | Lexical + semantic in one query; the chapter's engine |
| Graph | **Neo4j** (Community locally, AuraDB Free in production) + Cypher | Neptune equivalent with mature tooling |
| Relational | **PostgreSQL** (18 locally, Neon Free in production) | Users, tickets, messages, chunks, outbox, evals |
| Cache / pub-sub | **Valkey** locally, Render Key Value in production (Redis protocol) | Sessions, caches, coalescing, quotas, SSE fan-out |
| Object storage | S3 API: **Cloudflare R2** (prod), local filesystem/R2 dev bucket | 10 GB free, no egress fees |
| HTML parsing | `httpx`, `selectolax` / `trafilatura`, `tiktoken` | Parsing that preserves headings; accurate token counts |
| PII | **Presidio** (pattern recognizers only) + custom regex | Deterministic, light enough for 512 MB |
| Resilience | `tenacity` (backoff), a small custom circuit breaker | Trips on latency as well as errors |
| Email | **Resend** (3,000 / month, 100 / day free) | Verification codes |
| Bot protection | **Cloudflare Turnstile** (free) | Protects the login form and the OpenAI budget |
| Frontend | **Next.js** (App Router, **static export**), TypeScript, Tailwind CSS | Deploys as a free Render Static Site (CDN, no cold start) |
| Evaluation | pytest-based golden-set runner, Ragas-style metrics, custom judge prompts | The chapter's three-part evaluation |
| Observability | OpenTelemetry, structlog (JSON logs), **Grafana Cloud** free, **Langfuse Cloud** free | No self-hosted ClickHouse/Prometheus to fit on free compute |
| Testing | pytest, testcontainers, Playwright, **k6** load tests | Unit → integration → E2E → load |
| Delivery | Docker Compose (local), GitHub Actions (CI + ingestion cron), **Render Blueprint** (`render.yaml`) | Reproducible, infrastructure as code |

## 8. Low-level design

### 8.1 Repository layout

```
CustomerSupportAgent/
├─ backend/
│  ├─ app/                 # core API (one deployable)
│  │  ├─ main.py           # FastAPI app factory, health endpoints
│  │  ├─ config.py         # pydantic-settings
│  │  ├─ auth/  chat/  orchestrator/  retrieval/  genai/
│  │  ├─ tickets/  privacy/  ingest_api/
│  ├─ workers/             # scheduler, crawl_chunk, eeep, eval (batch jobs)
│  ├─ shared/              # db models, event schemas, logging
│  ├─ migrations/          # Alembic (Phase 1)
│  ├─ evals/               # golden dataset + runners
│  └─ tests/
├─ web/                    # Next.js: /widget, /console
├─ infra/docker-compose.yml
├─ docs/PLAN.md, docs/adr/
├─ render.yaml             # Render Blueprint
└─ .github/workflows/      # ci.yml, ingest.yml (Phase 1)
```

### 8.2 API design

**Auth**

| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/auth/email/start` | `{email, turnstile_token}` → sends a 6-digit code. The code is stored hashed in Key Value (TTL 10 min, max 5 attempts). Rate-limited per email and per IP. |
| POST | `/api/v1/auth/email/verify` | `{email, code}` → access JWT (15 min) + refresh token (httpOnly, `Secure`, `SameSite=Lax` cookie) |
| POST | `/api/v1/auth/refresh`, `/api/v1/auth/logout` | Refresh-token rotation and revocation |
| GET | `/api/v1/me` | Profile, role, remaining daily quota |

**Customer chat**

| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/chat/sessions` | Creates a session and its ticket (`OPEN`) |
| POST | `/api/v1/chat/sessions/{id}/messages` | `{text, version_hint?}` → `202 {message_id}` (the chapter's "working on it" step) |
| GET | `/api/v1/chat/sessions/{id}/stream` | SSE events: `ack`, `token`, `citations`, `final`, `escalated`, `agent_message`, `error` |
| POST | `/api/v1/chat/messages/{id}/feedback` | `{rating: 1 or -1, comment?}` |
| POST | `/api/v1/chat/sessions/{id}/escalate` | The user explicitly asks for a human |

`final` event payload (extends the chapter's response contract):

```json
{
  "message_id": "m_9",
  "answer": "An E11000 duplicate key error means ... [c:7f3a]",
  "citations": [{"chunk_id": "7f3a", "url": "https://www.mongodb.com/docs/...", "title": "Unique Indexes", "versions": ["8.0"]}],
  "confidence": 0.82,
  "route": "ANSWERED",
  "resolved_version": "8.0",
  "latency_ms": 2410
}
```

**Agent console** (requires `role=agent`)

| Method | Path |
|---|---|
| GET | `/api/v1/console/escalations?status=` |
| GET | `/api/v1/console/tickets/{id}` (transcript, summary, retrieved context, scores) |
| POST | `/api/v1/console/tickets/{id}/claim` · `/messages` · `/resolve` · `/kb-approve` |
| GET/POST/PATCH | `/api/v1/console/faqs` |
| GET | `/api/v1/console/review-queue` (low-confidence or thumbs-down answers) |

**Ingestion**

| Method | Path | Notes |
|---|---|---|
| POST | `/api/ingest/collect` | `{source_id, url, type: DOC or TICKET, op: UPSERT or DELETE}` → `{"status":"QUEUED"}`. Writes an **outbox** row; the next ingestion run publishes it to `raw_documents`. Authenticated with a shared secret. |

**Health:** `GET /healthz` (liveness) and `GET /readyz` (dependency checks).

### 8.3 Event schemas (topics)

```jsonc
// raw_documents — key = source_id (per-document ordering)
{"event_id":"uuid","source_id":"DOC:manual/core/index-unique","type":"DOC",
 "url":"https://www.mongodb.com/docs/manual/core/index-unique/","version":"8.0",
 "op":"UPSERT","lastmod":"2026-10-01T00:00:00Z","emitted_at":"..."}

// chunked_documents — key = doc_id
{"doc_id":"...","version":"8.0","chunk_ids":["7f3a","..."],"content_hash":"sha256:...","op":"UPSERT","kb_version":42}

// ticket_events
{"ticket_id":"t_1","event":"ESCALATED|RESOLVED|KB_APPROVED","at":"..."}
```

- Every topic has a matching `*.dlq` dead-letter topic.
- Consumers are **idempotent**: a write is skipped when the `content_hash` hasn't changed.
- Offsets are committed only after all stores are written, so delivery is at-least-once.

### 8.4 Data models

**PostgreSQL**

```sql
users(id uuid pk, email citext unique, role text check (role in ('customer','agent','admin')),
      verified_at timestamptz, daily_message_quota int, created_at timestamptz)
refresh_tokens(id uuid pk, user_id fk, token_hash text, expires_at, revoked_at)

documents(id text pk, url text, version text, title text, source_type text,   -- DOC | TICKET
          content_hash text, lastmod timestamptz, status text, security_level text, ingested_at)
chunks(id text pk,                       -- sha256(normalized text) prefix → cross-version dedupe
       text text, breadcrumb text, token_count int, kb_version int, created_at)
chunk_occurrences(chunk_id fk, doc_id fk, version text, ordinal int, primary key (chunk_id, doc_id))

tickets(id uuid pk, user_id fk, status text, version int,     -- optimistic lock
        resolved_version text, confidence_last real, escalation_reason text,
        assigned_agent_id fk, created_at, updated_at)
messages(id uuid pk, ticket_id fk, role text check (role in ('user','bot','agent','system')),
         content_masked text, citations jsonb, confidence real, model text,
         tokens_in int, tokens_out int, latency_ms int, created_at)        -- append-only
escalations(ticket_id pk fk, summary jsonb, reason text, created_at, claimed_at, resolved_at)
feedback(message_id fk, user_id fk, rating smallint, comment text, created_at)
faq_entries(id uuid pk, question_hash text unique, question text, answer text, citations jsonb, enabled bool)
ingest_outbox(id bigserial pk, payload jsonb, created_at, published_at)
golden_examples(id, query, expected_answer, category, expected_route, version)
eval_runs(id, started_at, git_sha, config jsonb) · eval_results(run_id, example_id, scores jsonb, passed bool)
llm_usage_daily(day date pk, cost_usd numeric)                              -- backs the spend cap
```

**OpenSearch index `kb_chunks`.** It keeps all of the chapter's `knowledge_chunks` fields and adds `text` and `error_codes` for the lexical half of hybrid search.

| Field | Type | Purpose |
|---|---|---|
| `chunk_id` | keyword | Primary key (shared with Postgres and Neo4j) |
| `doc_ids`, `urls`, `title`, `breadcrumb` | keyword / text | Grounding and display |
| `text` | text (English analyzer) | BM25 |
| `entities`, `error_codes`, `drivers` | keyword | Filters and boosts |
| `versions` | keyword | **Version filter** |
| `source_type`, `security_level` | keyword | Filtering (`PUBLIC` only today) |
| `update_timestamp`, `kb_version` | date / integer | Freshness |
| `embedding` | knn_vector (1536, HNSW, cosine) | Semantic search |

**Neo4j**

- **Nodes:**
  - `Document {id, url, title, versions}`, `Section {id, title, level}`, `Chunk {chunk_id, versions}`.
  - `Entity {id, canonical_name, aliases}`, with sub-labels `ErrorCode | Method | Operator | Command | Component | Driver | ConfigOption | Concept`.
  - `Ticket`, `Resolution` (from D8).
- **Relationships:**
  - From the chapter: `IS_PART_OF`, `REFERENCES`, `RESOLVES`, `HAS_ROOT_CAUSE`, `USES_COMPONENT`, `DEPENDS_ON`.
  - Added: `MENTIONS` (Chunk→Entity), capped at the top 8 per chunk to stay within free-tier quotas.
  - Every edge stores `{source_context: chunk_id, confidence, extracted_by: 'rule'|'llm', kb_version}` for traceability.
- **Constraints:** uniqueness on `id` per label; index on `Entity.canonical_name`.

**Key Value (Redis protocol) keys**

| Key | Contents | TTL |
|---|---|---|
| `sess:{id}` | Last N masked turns + rolling summary | 24 h |
| `otp:{email}` | Hashed code + attempt count | 10 min |
| `faq:{sha(normalized_q)}` | Cached FAQ answer | — |
| `resp:{sha(rewritten_q + version + model + kb_version)}` | Response cache; bumping `kb_version` invalidates all | 24 h |
| `inflight:{hash}` | Request coalescing (single-flight) | 60 s |
| `quota:{user}:{day}`, `spend:{day}` | Per-user quota, global spend cap | 48 h |
| `rl:{ip}`, `rl:{email}` | Rate limiting | window |

Key Value on Render's free plan is in-memory only. **Nothing durable lives only in Redis**: sessions can be rebuilt from `messages` in Postgres.

### 8.5 Ingestion pipeline

1. **Scheduler.**
   - For each configured version, read MongoDB's docs sitemap(s) and compare entries with `documents` by `lastmod` and hash.
   - Emit UPSERT events for new or changed pages and DELETE events for removed ones.
   - Drain `ingest_outbox` into the same topic.
   - Triggers: nightly cron, manual dispatch, or a webhook outbox row.
   - Freshness SLA: ≤ 24 h.
2. **Crawl + chunk worker.**
   - Fetch politely: robots.txt, about 2 req/s, cached ETags. Save the raw HTML snapshot to R2.
   - Parse into a heading tree. Chunk along headings at **256–512 tokens**, keep code blocks whole, and overlap about 10% only when a section has to be split.
   - Set the breadcrumb, e.g. `Indexes > Unique Indexes > Behavior`.
   - Redact PII.
   - `chunk_id = sha256(normalized text)[:16]`. If the chunk already exists, add a `chunk_occurrences` row for the new version (dedupe, D12). Otherwise insert it.
   - Emit to `chunked_documents`.
3. **EEEP worker** (entity extraction and embedding processor).
   - **Rule-based edges:** headings → `IS_PART_OF`; internal links → `REFERENCES`; regex for error codes (`E\d{5}`, plus names from the error-codes reference page), `db.collection.<method>()`, `$operators`, driver names.
   - **LLM edges:** the cheapest model with a strict JSON schema restricted to the entity and relation types above (`RESOLVES`, `HAS_ROOT_CAUSE`, `DEPENDS_ON`, `USES_COMPONENT`).
   - **Entity resolution:** normalize names, use an alias table, and MERGE on `canonical_name`.
   - **Embedding:** `breadcrumb + text + entities` (D4), batched.
   - **Writes:** upsert into OpenSearch and MERGE into Neo4j per document; a DELETE removes the chunk occurrences, and orphaned chunks are garbage-collected.
4. **Backfill:** run once locally, with the Batch API for extraction. Production datastores are then loaded directly.

### 8.6 Query pipeline (orchestrator)

```
on message(text, session_id, user):
  quota/spend check ─────────────────────────────── exceeded → friendly limit message
  mask PII → persist user message → push to sess:{id} → SSE "ack"            (< 300 ms)
  coalesce on hash(masked tail of history + text)  (join an identical in-flight request)

  T0  FAQ exact match (normalized text hash)            hit → stream cached answer
  T1  deterministic pre-checks: out-of-scope rules, "talk to a human",
      version/driver regex, frustration score            → may ESCALATE
  T2  query rewrite (cheapest model) unless turn 1 is self-contained
      → {standalone_query, entities, version?, intent}
  T3  response cache (standalone_query + version + model + kb_version)
  T4  hybrid search: BM25 + kNN, normalized & weighted (0.3 / 0.7, tuned),
      filters: versions, security_level → top 40 chunks; sum scores per document
  T5  graph expansion (budget 300 ms, ≤ 2 hops, LIMIT 30):
        entity anchors:  (e:ErrorCode {canonical_name:$code})<-[:RESOLVES|HAS_ROOT_CAUSE]-(c:Chunk)
        structural:      matched Chunk -[:IS_PART_OF]-> Section <-[:IS_PART_OF]- sibling Chunks
        references:      1 hop of REFERENCES from the top documents
        all filtered by version
  T6  merge, dedupe, rank, pack context (≈ 6k tokens) with chunk ids; fetch text from Postgres
  T7  version ambiguity: if no version is known and top chunks differ by version → ask one clarifying question
  gate: retrieval_confidence < τ_r → ESCALATE (no generation)
  T8  synthesize (frontier model, streamed), inline [c:id] citations, guardrail system prompt
  T9  validate: ≥ 1 citation, all citations ⊆ retrieved ids, not the fallback sentence
  score: confidence = w1·retrieval + w2·citation_validity + w3·model_self_report
  route: ANSWERED | ESCALATE → persist bot message, update ticket, SSE "final"
```

- **Confidence (resolves G2).**
  - **Retrieval component:** the normalized top hybrid score, the margin between the top two documents, and whether a graph path was found.
  - **Citation validity:** 0 or 1.
  - **Model self-report:** a field in the structured output.
  - The weights and thresholds `τ_r` and `τ` are fitted on the golden set to maximize escalation recall at acceptable precision.
- **Escalation triggers:**
  - out-of-scope;
  - explicit request for a human;
  - retrieval gate fails;
  - citation validation fails;
  - `confidence < τ`;
  - frustration over 2 or more turns;
  - more than 3 bot turns without the user confirming resolution;
  - LLM unavailable after all fallbacks.
- **Streaming vs. citations (resolves G7).** The gate before generation stops most uncited answers before they start. In the rare case where validation fails after streaming, the streamed text is replaced with the standard fallback message and the ticket is escalated.
- **Escalation summary** (cheapest model, structured): `{issue, environment: {server_version, driver, deployment}, steps_tried, bot_answers: [{summary, citations}], sentiment, suggested_next_step}`.

### 8.7 Ticket state machine

```
OPEN ──► BOT_ACTIVE ──► RESOLVED_BY_BOT ──► CLOSED
              │   └──(idle 30 min)──► ABANDONED
              └──► ESCALATED ──► AGENT_ACTIVE ──► RESOLVED ──► CLOSED
                                                     └──(kb-approve)──► ticket_events → ingestion (D8)
```

- All transitions go through one module, with optimistic locking on `tickets.version`.
- In `AGENT_ACTIVE` the bot pipeline is bypassed. Agent messages reach the widget through Key Value pub/sub → SSE.

### 8.8 GenAI service: resilience

- **Model registry:** `task → [primary, fallbacks...]`, configured via environment variables.
- **Circuit breaker per model:**
  - Opens when, over a 60 s window, time-to-first-token exceeds 5 s or more than 20% of calls error.
  - After 30 s it lets one test call through and closes again if it succeeds.
- **Retries:** `tenacity` exponential backoff with jitter, 2 attempts, only on 429 and 5xx.
- **Fallback:** synthesis can fall back to a cheaper tier. If everything fails, the ticket escalates with a graceful message.
- **Cost:** token usage is converted to cost per call and accumulated in `spend:{day}` and `llm_usage_daily`. The global cap stops all LLM calls once reached.

### 8.9 Security and privacy

- **PII masking in both directions** before anything is stored, logged or sent to a model: `<EMAIL_MASKED>`, `<CONNECTION_STRING_MASKED>`, `<API_KEY_MASKED>`, `<CARD_MASKED>`. MongoDB connection strings that include passwords are the main risk in this domain.
- **Data handling:**
  - A `security_level` filter on every search.
  - Retention: messages deleted after 90 days (configurable).
  - A delete-my-data endpoint (GDPR).
- **Prompt injection:** retrieved content is clearly delimited and never treated as instructions. Markdown is sanitized on render.
- **Login protection:** JWTs are short-lived, refresh tokens are rotated and revocable, Turnstile protects the login form, and rate limits apply per email and per IP.
- **Network:** CORS is restricted to the UI origin. The UI and API share a custom domain (e.g. `support.example.com` and `api.support.example.com`). `onrender.com` is a public suffix, so cookies can't be shared across `*.onrender.com` services.
- **Secrets:** only in `.env` locally (git-ignored) and Render environment groups in production.

## 9. Deployment and hosting

### 9.1 Environments

| Concern | Local (Docker Compose) | Production (free tiers) |
|---|---|---|
| Core API | `uvicorn` on the host or the `app` profile | **Render Web Service (free)**, Docker, 512 MB / 0.1 CPU |
| Web UI | `next dev` | **Render Static Site** (free, CDN, no cold start) |
| Postgres | `postgres:18` | **Neon Free** (0.5 GB). Render's free Postgres expires after 30 days. |
| Cache / pub-sub | `valkey/valkey:9.1` | **Render Key Value (free)**, in-memory |
| Search | `opensearchproject/opensearch:3.9.0` | **Aiven for OpenSearch Free** (4 GB RAM, 20 GB, k-NN enabled) |
| Graph | `neo4j:2026.09.0-community` | **Neo4j AuraDB Free** (200k nodes, 400k relationships) |
| Event bus | `redpandadata/redpanda:v26.2.5` | Redpanda **service container inside the GitHub Actions ingestion job** |
| Ingestion workers | Compose or CLI | **GitHub Actions** cron + manual dispatch (Render workers aren't free) |
| Object storage | local bucket/dir | **Cloudflare R2** (10 GB) |
| Email / bot protection | Resend test mode / Turnstile test keys | Resend + Turnstile |
| Observability | console logs | Grafana Cloud free + Langfuse Cloud free |

### 9.2 Render free-tier constraints and mitigations

| Constraint | Mitigation |
|---|---|
| The web service **sleeps after 15 min idle and takes about 1 min to wake** | The widget shows a "waking up the demo…" state and calls `/healthz` on page load. Optionally an external uptime ping keeps it warm. One service running all month (~744 h) stays within the 750 free instance-hours. |
| 512 MB RAM, 0.1 CPU | No ML models in the API image (no spaCy, no local rerankers). Heavy work runs in the ingestion jobs. Two Uvicorn workers maximum. |
| Free instances can't be background workers or cron jobs | Ingestion runs in GitHub Actions. |
| Free Key Value is not persistent | Postgres is the source of truth; Redis only holds caches and ephemeral state. |
| Outbound bandwidth on the newer Hobby workspace (sources report as low as 5 GB/month) | SSE text payloads are small; static assets are served from the CDN; to be monitored. |

### 9.3 Free-tier capacity check

| Store | Estimated need (3 versions, deduplicated) | Free limit |
|---|---|---|
| Neon Postgres | ~90k chunks × ~2 KB ≈ 180 MB + app data | 0.5 GB |
| AuraDB nodes | ~90k chunks + ~25k sections + ~18k docs + ~20k entities ≈ 150k | 200k |
| AuraDB relationships | IS_PART_OF ~115k + MENTIONS (≤ 8/chunk, avg ~2.5) ~225k + others ~40k ≈ 380k | 400k (**tight**) |
| Aiven OpenSearch | ~90k × 1536-d float vectors ≈ 0.55 GB + text ≈ 1 GB | 20 GB / 4 GB RAM |

If AuraDB fills up: lower the MENTIONS cap, drop the oldest version, or skip Chunk nodes for boilerplate pages.

## 10. Non-functional targets, monitoring and alerting

**Latency budgets (resolve G1).** Measured with the service warm; free-tier cold starts are excluded and reported separately.

| Metric | Target (P90) |
|---|---|
| Acknowledgement | < 300 ms |
| Time to first answer token | < 3 s |
| Full answer | < 10 s |
| Escalation decision | < 60 s |
| Retrieval (OpenSearch + Neo4j) | < 1.2 s |

**Rough budget for one turn:**

| Step | Approx. time |
|---|---|
| PII masking | 20 ms |
| FAQ check | 10 ms |
| Query rewrite | 400–700 ms |
| Embedding | 150 ms |
| Hybrid search | 150 ms |
| Graph expansion | 300 ms |
| Chunk fetch | 30 ms |
| Synthesis, time to first token | 800–1,500 ms |

**Other targets**
- Containment (answered without escalation, on in-scope questions): ≥ 70%, as in the chapter.
- Groundedness ≥ 90%.
- Escalation recall on must-escalate cases ≥ 95%.
- Freshness ≤ 24 h.

**Alerts** (the chapter's thresholds, adapted)

| Area | Alert |
|---|---|
| Query path | P90 time to first token > 3 s; P90 full answer > 10 s; retrieval > 1.2 s; LLM time to first token > 1 s or total call time > 4 s (also trips the breaker) |
| Ingestion | Topic lag > 200 (warn) / > 5,000 (critical); end-to-end > 30 min per document; embedding P95 > 500 ms; ingestion job failure |
| Quality | Groundedness < 90%; thumbs-down > 5%; escalation rate > baseline + 20% |
| Cost | Daily spend > 150% of baseline; spend cap reached |
| System | 5xx > 1%; memory > 90%; dependency connection failures |

## 11. Evaluation strategy

- **Golden dataset v0:** about 150 examples covering the chapter's four categories (simple FAQ, complex synthesis, error code / graph traversal, must-escalate), each tagged with a version. Stored as YAML in `backend/evals/` and loaded into Postgres.
- **Offline gate (CI):**
  - Retrieval recall@k.
  - Faithfulness / groundedness (judge).
  - Answer correctness against the golden answer (1–5 judge rubric from the chapter).
  - Routing accuracy and escalation recall.
  - The build fails on a regression.
- **Per-task model evals (§5):** small labeled sets for rewrite, extraction and summary, used to pick the cheapest passing model.
- **Online:**
  - Judge groundedness on a sample of live answers.
  - Thumbs up/down.
  - Low-confidence and negative answers go to the console review queue (human in the loop).
- **GraphRAG proof:** the Phase 2 vs. Phase 3 comparison on the same golden set, published as part of the portfolio write-up.

## 12. Failure mode analysis

| Component | Failure | Impact | Mitigation |
|---|---|---|---|
| LLM | Hallucination | Confident wrong advice | Retrieval gate, mandatory valid citations, guardrail prompt, judge sampling, review queue |
| LLM | Latency spike / outage | User abandons the chat | Streaming, breaker → fallback tier → graceful escalation |
| LLM | Runaway cost / abuse | Budget exhausted | Email verification, Turnstile, per-user quota, global spend cap |
| OpenSearch (Aiven) | Paused or unavailable | No retrieval | Readiness check; FAQ cache still answers; otherwise escalate with a message |
| Neo4j (AuraDB) | Unavailable or slow | No graph expansion | Graph step is time-boxed and optional; continue with hybrid-search results |
| Key Value | Restart (in-memory) | Lost caches and sessions | Rebuild sessions from Postgres; caches warm up again |
| Postgres (Neon) | Cold start / unavailable | Cannot persist | Connection retry; return 503 with a friendly UI state |
| Render API | Cold start (~1 min) | Slow first visit | "Waking up" UI state, optional keep-warm ping |
| Ingestion | Job failure / bad parse | Stale or corrupt KB | Idempotent consumers, DLQ topics, alerts, `kb_version` rollback |
| MongoDB docs site | Layout change | Parser breaks | Parser contract tests on saved HTML fixtures; failed pages go to the DLQ, not partial writes |

## 13. Delivery phases

Every phase ends with something demo-able for the portfolio.

| Phase | Deliverable | Exit criteria |
|---|---|---|
| **0. Foundation** | Monorepo, Compose stack, FastAPI skeleton, Next.js skeleton, CI, ADRs, Render Blueprint | `make up` gives a healthy stack; `make test` passes; CI green |
| **1. Ingestion v1** | Multi-version sitemap crawl → chunk → dedupe → embed → OpenSearch + Postgres; outbox; GitHub Actions ingestion job; golden set v0 | All configured versions indexed; re-run is a no-op; nightly diff works |
| **2. Query v1 (hybrid RAG)** | Chat API, SSE, widget, citations, guardrails, version filter, cost metering | Golden-set baseline recorded; warm first token under 3 s (P90) |
| **3. GraphRAG** | EEEP rules + LLM extraction, Neo4j, graph expansion | Measurable lift over Phase 2 on the error-code and complex categories |
| **4. Auth, tickets, escalation** | Email-code login, quotas, state machine, calibrated confidence, summaries, agent console, live takeover | Escalation recall ≥ 0.95; end-to-end handoff demo |
| **5. Hardening** | PII middleware, caches and coalescing, breaker and fallback, rate limits, observability, judge sampling, KB feedback loop | Simulated LLM outage handled; dashboards and alerts live |
| **6. Deploy and showcase** | Render + free datastores live, k6 load test, security review, portfolio write-up with eval results | Public demo URL; targets met when warm |

## 14. Risks and open items

| Item | Status / action |
|---|---|
| MongoDB docs licence terms | Verify CC BY-NC-SA and attribution wording before going public |
| MongoDB docs URL / sitemap scheme per version | Investigate at the start of Phase 1 |
| AuraDB relationship quota is tight | Monitor during the Phase 3 backfill; mitigations in §9.3 |
| Render Hobby bandwidth and cold starts | Monitor; keep-warm ping if needed |
| Aiven free OpenSearch may pause when inactive | Readiness check + resume procedure in the runbook |
| Free-tier terms change without notice | Everything is reproducible from `render.yaml` + Compose; datastores can be swapped via environment variables |
| OpenAI model IDs and prices change | Model IDs are config only; per-task evals rerun on change |
