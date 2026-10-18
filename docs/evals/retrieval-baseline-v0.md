# Retrieval baseline — golden set v0 (Phase 1)

The bar that Phase 2 (query rewriting, reranking) and Phase 3 (graph expansion) must beat on the same golden set ([PLAN §11](../PLAN.md#11-evaluation-strategy)).

| | |
|---|---|
| Date | 2026-10-18 |
| Golden set | `backend/evals/golden/v0.yaml`: 51 examples, 41 answerable (10 must-escalate are excluded from retrieval metrics) |
| Corpus | MongoDB docs 9.0 / 8.0 / 7.0: 4,822 active pages, 23,797 chunks (ingest run #5) |
| Embeddings | `text-embedding-3-small`, 1536 dimensions |
| Retrieval | OpenSearch hybrid (BM25 + k-NN), filtered to the example's docs version, top 10 chunks |
| Command | `make eval-retrieval` (`make eval-validate`: 0 problems) |

## Results

A hit means at least one of the example's expected source pages appears among the top-k **chunks**.

| Slice | n | R@1 | R@3 | R@5 | R@10 | MRR@10 |
|---|---|---|---|---|---|---|
| **overall** | 41 | 0.22 | 0.44 | 0.46 | 0.56 | 0.34 |
| complex | 15 | 0.33 | 0.67 | 0.73 | 0.80 | 0.51 |
| error_code | 11 | 0.09 | 0.36 | 0.36 | 0.45 | 0.23 |
| faq | 15 | 0.20 | 0.27 | 0.27 | 0.40 | 0.24 |

## Reading the misses

22 of 41 examples have no expected source in the top 5. Looking at what came back instead:

- **Labels stricter than the docs (≈10).** The search returns a page that answers the question just as well as the labelled one, usually a tutorial instead of the API reference page. For example, `faq-insert-one` expects `reference/method/db.collection.insertone` and gets `tutorial/insert-documents`. Similar cases: `faq-update-set`, `faq-delete-many`, `faq-create-user`, `faq-ttl`, `faq-explain`, `faq-create-index`, `complex-embed-vs-reference`. This mostly explains why FAQ scores below complex.
- **Genuine retrieval misses (≈12).** Off-topic pages for short or conversational queries (`faq-change-streams`, `error-e11000-multiturn`), the wrong auth mechanism (`faq-enable-auth` → LDAP), an error page instead of the topic (`error-auth-failed` → `meta/401`), and close-but-wrong references for error messages (`error-not-primary`, `error-sort-memory`, `error-max-time`, `complex-agg-memory`).
- **Chunk crowding.** The top 5 chunks cover only 3.6 distinct pages on average, because several chunks of one long page often outrank everything else.

## Follow-ups

1. **Review v0 labels:** add equally valid source pages to `expected_sources` (each checked by `eval-validate` for the key facts), then re-record this baseline. Done as a reviewed change, not silently, because it moves the bar.
2. **Phase 2 levers** this baseline points at: page-level diversification of the top k, query rewriting for conversational and multi-turn queries, and reranking.
3. **Phase 3:** error-code examples score lowest (R@5 0.36), which is the category graph expansion is meant to lift.
