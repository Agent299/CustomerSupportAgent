# Does GraphRAG fix retrieval? A study on golden set v0

> **Outcome (2026-10-18):** option B adopted, with `gpt-4.1-nano` as the plan model and the kNN half searching on the user's own words. See [ADR 0017](../adr/0017-query-expansion-and-identifier-anchors.md) for the production measurements.

**Question:** the answer bar is not met, and part of the gap is retrieval (error-code context recall 0.64, see [answers-baseline-v0](../evals/answers-baseline-v0.md)). Would graph-based retrieval (GraphRAG, Phase 3) close it, compared with cheaper alternatives?

**Short answer:** not the graph itself. On this corpus and golden set, link-graph expansion (1-hop neighbours, personalized PageRank) did not improve ranking or recall. Two cheaper things did:

1. **LLM query expansion**: the cheapest model (`gpt-6-luna`) names the concepts and features the answer is documented under, and the terms are added to the query. Context recall rose from **0.83 to 0.93**, R@5 from 0.71 to 0.83, and fact recall from 0.84 to 0.94.
2. **Deterministic identifier anchors**: an index from identifiers such as `NotWritablePrimary` or `$median` to the pages that define them or mention them most (an entity graph without the link graph). Error-code fact recall rose from **0.59 to 0.86** at no cost.

Together, context recall reaches **0.95** (error codes **0.91**), and misses fall from 7 examples to 2. The knowledge GraphRAG was meant to add, e.g. "E11000 → duplicate key → unique index", came from the model's own knowledge at query time rather than from an explicit graph.

| | |
|---|---|
| Date | 2026-10-18 |
| Data | 41 answerable golden examples; 4,822 pages, 23,797 chunks carrying 58,044 links to other pages (ingest run #5) |
| Code | `backend/research/graphrag_study.py` (research only, not wired into the app) |
| Raw results | `graphrag-study-run1.json`, `graphrag-study-run2.json` (two runs; LLM steps vary) |
| LLM cost | $0.027 per full run (expansion + rerank for 41 queries) |

## Method

Every method returns a ranked passage list that goes through the **same packing as the live pipeline**: at most 2 chunks per page, a 6k-token context. Each list is then scored the same way:

- **R@k, MRR@10:** rank of the first passage from an expected page in the packed list.
- **Context recall:** an expected page is in the packed context.
- **Fact recall:** the share of the example's key facts (verbatim doc quotes) present in the packed context text. This is the closest proxy for "could the model have said it", and it catches chunk-level misses that page-level recall hides.

Methods compared:

| Method | What it does |
|---|---|
| base | Live pipeline: hybrid BM25 + kNN (weights 0.3/0.7), top 40 |
| w=0.1 / 0.5 / 0.7 | Same, with BM25 weight 0.1 / 0.5 / 0.7 |
| **Graph** | |
| link1 | Pages linked to or from the top 10 pages, scored by seed rank × link count, damped for hub pages (glossary, install guides) |
| ppr | Personalized PageRank over the undirected page-link graph, seeded by the top 10 pages |
| entity | Identifier anchors: pages whose title defines a query identifier, then pages where it is most concentrated (identifiers on > 2 % of pages ignored) |
| entity+ppr | Both, PPR also seeded from the anchor pages |
| +siblings | The chunks before and after each of the top 3 passages (section context) |
| **LLM, cheapest model** | |
| llm-expand | Adds model-suggested doc terms to the query (BM25 and embedding) |
| llm-rerank | Model reorders the top 40 passages |
| Combinations | Fused with reciprocal-rank fusion (RRF); graph pages are represented by their best chunks for the query |

## Results (run 1)

| Method | R@1 | R@5 | MRR@10 | Context recall | Fact recall | Error-code context | Error-code facts |
|---|---|---|---|---|---|---|---|
| base | 0.46 | 0.71 | 0.57 | 0.83 | 0.84 | 0.64 | 0.59 |
| w=0.1 | 0.41 | 0.73 | 0.56 | 0.85 | 0.85 | 0.64 | 0.59 |
| w=0.5 | 0.44 | 0.61 | 0.51 | 0.83 | 0.87 | 0.64 | 0.68 |
| w=0.7 | 0.24 | 0.46 | 0.36 | 0.83 | 0.83 | 0.64 | 0.68 |
| link1 | 0.44 | 0.71 | 0.54 | 0.88 | 0.83 | 0.64 | 0.59 |
| ppr | 0.44 | 0.71 | 0.54 | 0.83 | 0.83 | 0.64 | 0.64 |
| entity | 0.46 | 0.71 | 0.57 | 0.85 | 0.91 | 0.73 | 0.86 |
| entity+ppr | 0.44 | 0.71 | 0.54 | 0.85 | 0.89 | 0.73 | 0.86 |
| entity+ppr+siblings | 0.44 | 0.66 | 0.51 | 0.85 | 0.87 | 0.73 | 0.86 |
| base+siblings | 0.46 | 0.68 | 0.53 | 0.83 | 0.82 | 0.64 | 0.59 |
| llm-rerank | **0.66** | 0.80 | 0.72 | 0.83 | 0.88 | 0.64 | 0.68 |
| llm-expand | 0.59 | 0.83 | 0.69 | 0.93 | 0.94 | 0.82 | 0.86 |
| **llm-expand+entity** | 0.56 | 0.80 | 0.67 | **0.95** | **0.96** | **0.91** | **0.95** |
| llm-expand+rerank | 0.61 | **0.88** | **0.73** | 0.93 | 0.95 | 0.82 | 0.86 |
| llm-expand+entity+ppr | 0.56 | 0.76 | 0.65 | 0.95 | 0.96 | 0.91 | 0.95 |

Run 2 agrees within ±0.03 on every LLM row (e.g. llm-expand+entity: context 0.95, facts 0.95; llm-expand+rerank: R@1 0.68, MRR 0.77). `base (no packing)`, i.e. the raw top 10 chunks, reproduces the Phase 1 retrieval baseline (R@5 0.68).

## What happened, example by example

- **The link graph doesn't carry the missing relations.** The pages that mention `E11000` (insertMany, bulkWrite, insert) mostly link to generic pages (transactions, glossary, install guides, write concern), not to `core/index-unique`, and the expected page never mentions "E11000" or "duplicate key" itself. PPR and 1-hop expansion mostly reshuffle pages that are already retrieved: link1 gains 2 FAQ pages in context and loses ranks elsewhere.
- **The error-code table in the source has no codes.** In MongoDB's official `.md` export of `reference/error-codes`, the *Code* column is empty (`|  | DuplicateKey |`); the numbers exist only on the HTML page. No lexical or graph method can link "11000" to "DuplicateKey" from this corpus as ingested.
- **Identifier anchors** fix `error-not-primary` (the page that defines `NotWritablePrimary` enters at rank 2) and bring in pages that state the E11000 facts. With no identifiers in the query they change nothing.
- **Query expansion** fixes what needs background knowledge: `error-e11000` and the multi-turn variant ("duplicate key, unique index"), `faq-change-streams` (rank 20 → 1), `complex-find-sort-slow` (rank 12 → 2, ESR guideline), `faq-enable-auth` (13 → 3), `faq-delete-many`, `complex-schema-validation`. Two small losses: `complex-embed-vs-reference` (rank 1 → 6, still in context) and `error-doc-too-large` (3 → 7).
- **Still missing with llm-expand+entity:** `faq-update-set` (the tutorial outranks the `$set` page) and `error-auth-failed` (the connection-string page), although the auth-failed key facts are now in the context.
- **Section siblings hurt:** within a fixed 6k budget they push out other pages.

## Costs and latency

| Step | Cost per query | Added latency (p50 / p90) |
|---|---|---|
| Identifier anchors | 0 (in-memory index, built at ingestion) | ~ms |
| LLM expansion (`gpt-6-luna`, effort low) | ≈ $0.0003 | 1.8 s / 2.1 s |
| LLM rerank of 40 passages | ≈ $0.0003 | 1.6–2.1 s / 3.6–4.1 s |

Expansion runs **before** retrieval, so it adds directly to time to first token (TTFT; today's p90 is 1.9 s, the target is < 3 s). Ways to keep it inside the budget, none of them tested here:
- fold it into the multi-turn rewrite call that already exists, which then runs on every turn;
- start the plain hybrid search in parallel and fuse when the expansion arrives;
- use `reasoning_effort=minimal` or a non-reasoning small model;
- cache expansions per normalized query.

## Caveats

- **Small sample.** n = 41 (11 error-code examples); one example moves a category score by 0.07–0.09. The direction is consistent across both runs and most per-example changes are large (rank 20 → 1, not 3 → 2), but treat the numbers as ±0.05.
- **Tuning on the same set.** The anchors' > 2 % page-share filter was added after the first pass showed "MongoDB" being treated as an identifier. The fix is generic, but it was found on this set. The expansion and rerank prompts were written once and not tuned.
- **Not tested:** the full Phase 3 design, an **LLM-extracted entity/relation graph** (about $1 with the Batch API for 23.8k chunks). It could add error → concept edges that the link graph lacks. But query expansion already supplies those relations at query time for $0.0003, so the extracted graph would need to beat 0.95 context recall to be worth it.
- **Retrieval only.** These are retrieval metrics. Whether higher fact recall raises the judge's correctness score needs an `eval-answers` run with the chosen method (≈ $0.45).

## Options for the decision

| Option | Expected effect | Cost |
|---|---|---|
| A. Identifier anchors only | Error-code facts 0.59 → 0.86; no latency | Deterministic; an index built at ingestion |
| B. A + LLM query expansion | Context recall 0.83 → 0.95, facts 0.84 → 0.96 | ≈ $0.0003/query; +~2 s before first token unless parallelized |
| C. B + LLM rerank | Best ranking (R@5 0.88, MRR 0.73–0.77) | Another ~2–4 s; context recall unchanged |
| D. Full GraphRAG (Neo4j + LLM entity extraction) | Unknown; the link-graph part showed no gain here | Days of work + ≈ $1 extraction; not justified by this study |
