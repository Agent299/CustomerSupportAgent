# ADR 0017: Query expansion and identifier anchors instead of a link graph

- Status: Accepted
- Date: 2026-10-18
- Changes: the Phase 3 plan in [PLAN §13](../PLAN.md#13-delivery-phases) (GraphRAG); [ADR 0006](0006-entity-extraction-rules-plus-llm.md) is deferred, not superseded

## Context

The chapter's design expands retrieval through a knowledge graph in Neo4j. Phase 2 left two gaps the graph was meant to close: error-code questions (context recall 0.64) and pages that answer a question without sharing its words.

The [retrieval study](../research/graphrag-retrieval-study.md) tested the graph ideas against cheaper ones on the 41 answerable golden examples:

- **Following page links** (1-hop neighbours, personalized PageRank) gave no gain. Pages that mention E11000 link to generic pages (glossary, transactions), never to the unique-index page.
- **Identifier anchors**, a deterministic index from names like `NotWritablePrimary` or `$median` to the pages that define them, raised error-code fact recall from 0.59 to 0.86 at no latency cost.
- **LLM query expansion**, where the cheapest model names the concepts an answer is documented under ("E11000 → duplicate key, unique index"), supplied the missing link from the model's own knowledge. With anchors, context recall went from 0.83 to 0.95.

The owner chose this combination (the study's option B).

## Decision

- **One "plan" call per message** (`MODEL_EASY`) returns a standalone query and expansion terms. It replaces the multi-turn-only rewrite. It runs **concurrently** with the embedding of the user's turns and the anchor lookup, under a 2.5 s limit. If it fails or runs late, search uses the raw turns.
- **Lexical search uses the plan; semantic search does not.** BM25 gets the query plus the terms, while the kNN vector is the embedding of the user's own turns, computed while the plan call runs. That keeps the plan off the critical path for the embedding. The study embedded the expanded text instead; the measurements below use this design.
- **Identifier anchors** are computed at ingestion into Postgres (`identifier_anchors`, rebuilt by `make anchors` and at the end of `make ingest`). At query time, the anchored pages' best chunks are fused with the main search by reciprocal rank.
- **Plan model: `gpt-4.1-nano`** (not a reasoning model; $0.10 / $0.40 per 1M tokens). Measured on the same set, it was the fastest candidate (0.71 s p50 for the call):

  | Configuration | Context recall | Fact recall | Correct answers | TTFT p90 |
  |---|---|---|---|---|
  | Phase 2 (rewrite multi-turn only) | 0.85 | 0.87 | 0.66 | 2.04 s (API) |
  | Anchors only | 0.85 | 0.91 | 0.73 | ≈ 1.7 s |
  | **Nano plan + anchors (chosen)** | **0.93** | **0.91** | **0.76** | **1.77 s (API)** |
  | `gpt-6-luna` plan (effort `none`) + anchors | 0.98–1.00 | 0.95 | 0.76 | ≈ 2.6 s, retrieval p90 2.07 s |

  Luna retrieves more, but answers were no more correct and the extra 0.8 s would take most of the latency budget. "Correct answers" is judged by `gpt-6-astra` over all answerable examples, an escalation counting as a miss; treat differences under ±0.05 as noise.
- **No Neo4j for retrieval now.** LLM entity extraction and graph expansion (ADR 0006, PLAN §8.6 T5) are deferred until an eval shows a gap that anchors plus expansion cannot close.

## Consequences

- Every message costs one extra small-model call (≈ $0.00005), and retrieval takes about 0.4 s longer at the median (0.78 s against 0.38 s). Time to first token stays within the 3 s target.
- `QUERY_EXPANSION=false` turns expansion off; multi-turn messages still get a rewrite.
- The anchor index must be rebuilt after ingestion. `make ingest` does it; a webhook-only update leaves it slightly stale until the next run, which only affects new identifiers.
- Phase 3's deliverable changes from "GraphRAG" to closing the answer bar. The remaining misses are reference-answer precision, `faq-update-set`, `complex-schema-validation` and `error-auth-failed`, and answers that leave out caveats ([answers-baseline-v0](../evals/answers-baseline-v0.md)).
- The Neo4j container stays in Compose, unused, until a later ADR uses or removes it.
