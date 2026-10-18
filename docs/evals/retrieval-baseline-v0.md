# Retrieval baseline — golden set v0 (Phase 1)

The bar that Phase 2 (query rewriting, reranking) and Phase 3 (graph expansion) must beat on the same golden set ([PLAN §11](../PLAN.md#11-evaluation-strategy)).

| | |
|---|---|
| Date | 2026-10-18 (after the label review below) |
| Golden set | `backend/evals/golden/v0.yaml`: 51 examples, 41 answerable (10 must-escalate are excluded from retrieval metrics) |
| Corpus | MongoDB docs 9.0 / 8.0 / 7.0: 4,822 active pages, 23,797 chunks (ingest run #5) |
| Embeddings | `text-embedding-3-small`, 1536 dimensions |
| Retrieval | OpenSearch hybrid (BM25 + k-NN), filtered to the example's docs version, top 10 chunks |
| Command | `make eval-retrieval` (`make eval-validate`: 0 problems) |

## Results

A hit means at least one of the example's expected source pages appears among the top-k **chunks**.

| Slice | n | R@1 | R@3 | R@5 | R@10 | MRR@10 |
|---|---|---|---|---|---|---|
| **overall** | 41 | 0.44 | 0.66 | 0.68 | 0.73 | 0.55 |
| complex | 15 | 0.53 | 0.80 | 0.87 | 0.87 | 0.67 |
| error_code | 11 | 0.27 | 0.55 | 0.55 | 0.64 | 0.41 |
| faq | 15 | 0.47 | 0.60 | 0.60 | 0.67 | 0.53 |

## Label review

The first run (same index, same queries) scored recall@5 **0.46** and MRR@10 **0.34** overall (faq R@5 0.27). About half of the misses were labels, not retrieval: the search returned a page that answers the question as well as the labelled one, usually the tutorial instead of the API reference.

The review applied one rule to the top-10 retrieved pages of **all 41** answerable examples, hits and misses alike:

1. **Deterministic:** the page must contain every key fact of the example on its own.
2. **Judgement:** the page must be about the question, not merely mention the facts.

Step 1 produced 49 candidate pages across 21 examples; step 2 kept **23 pages on 15 examples** (marked `# label review 2026-10-18` in the YAML). Examples of rejections: pages that mention `27017` but are not about the default port (`reference/mongodb-wire-protocol`), sharded- or time-series-specific variants of a general question, the LDAP tutorial for "turn on authentication", and `findOneAnd*` method pages for a `maxTimeMS` error. Pages that answer the question but phrase the key facts differently (e.g. `tutorial/update-documents` for `faq-update-set`) were **not** added: the key facts stay the contract.

## Remaining misses

13 of 41 examples have no expected source in the top 5:

- **Conversational or vague queries pull in off-topic pages:** `faq-change-streams`, `error-e11000-multiturn`, `error-auth-failed` (returns `meta/401`), `faq-replica-set`.
- **The right topic, the wrong page:** `faq-update-set` and `faq-delete-many` (tutorials rank above the operator and method pages), `faq-enable-auth` (LDAP setup), `complex-schema-validation` and `error-validation-failed` (neighbouring schema-validation pages), `complex-find-sort-slow` (slow-query troubleshooting instead of the ESR guideline).
- **Error strings without their topic:** `error-e11000` (returns the generic error-code table), `error-not-primary` (read-preference pages), `faq-bulkwrite-80` (general bulk-write pages and the `db.collection.bulkWrite()` shell method rank above the 8.0 `bulkWrite` command; rank 6).
- **Chunk crowding:** the top 5 chunks cover only 3.6 distinct pages on average, because several chunks of one long page often outrank everything else.

## What this points at

- **Phase 2:** page-level diversification of the top k, query rewriting for conversational and multi-turn queries, and reranking.
- **Phase 3:** error-code examples score lowest (R@5 0.55); linking an error string to its concept page is the job graph expansion is meant to do.
