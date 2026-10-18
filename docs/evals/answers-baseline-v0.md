# Answer baseline — golden set v0 (Phase 2 · 1/3)

End-to-end quality of the headless query pipeline (`app/orchestrator/pipeline.py`) on all 51 golden examples, and how the answer model was chosen ([PLAN §5](../PLAN.md#5-llm-usage-policy), [§11](../PLAN.md#11-evaluation-strategy)).

| | |
|---|---|
| Date | 2026-10-18 |
| Pipeline | rules → version → rewrite (multi-turn only) → hybrid search (top 40, ≤ 2 chunks per page, 6k-token context) → streamed answer with `[c:id]` citations → citation validation |
| Models | rewrite and answer `gpt-6-luna` (`reasoning_effort=low`); judge `gpt-6-astra` |
| Command | `make eval-answers` |

## Baseline (chosen configuration)

| Slice | n | Route | Context recall | Answered | Citation hit | Judge (1–5) | Correct | TTFT p90 |
|---|---|---|---|---|---|---|---|---|
| **overall** | 51 | 0.98 | 0.83 | 0.98 | 0.85 | 3.95 | 0.66 | 1.9 s |
| complex | 15 | 1.00 | 0.93 | 1.00 | 0.93 | 3.87 | 0.67 | 2.1 s |
| error_code | 11 | 0.91 | 0.64 | 0.91 | 0.70 | 3.80 | 0.45 | 1.5 s |
| escalation | 10 | 1.00 | – | – | – | – | – | 1.1 s |
| faq | 15 | 1.00 | 0.87 | 1.00 | 0.87 | 4.13 | 0.80 | 1.9 s |

- **Route:** expected ANSWERED vs ESCALATE. Escalation recall is 1.00: 9 of 10 caught by the deterministic rules, the nginx question by the model's fallback reply. The one wrong route is `error-e11000`, escalated because retrieval missed the unique-index page.
- **Context recall:** an expected page reached the model. 0.83–0.85 against a top-10 search recall of 0.73 in the [retrieval baseline](retrieval-baseline-v0.md): page diversification and the multi-turn rewrite help.
- **Correct:** answerable examples answered with a judge score ≥ 4; an escalation counts as a miss.
- **TTFT:** time to first token, measured from the start of the request, with 4 examples running in parallel.
- **Cost:** about $0.001 per answer with `gpt-6-luna`; a full eval run costs about $0.44, almost all of it for the judge.

## After query expansion and identifier anchors (ADR 0017)

Same golden set, judge and answer model (`gpt-6-luna`, effort `low`). The search plan uses `gpt-4.1-nano`.

| Slice | n | Route | Context recall | Citation hit | Judge (1–5) | Correct |
|---|---|---|---|---|---|---|
| **overall** | 51 | **1.00** | 0.93 | 0.85 | 4.07 | **0.76** (was 0.66) |

Through the API: time to first token 1.55 s p50, **1.77 s p90**; full answer 3.71 s p90. The bar (≥ 0.85 correct when the context has the page) is still **not met**: 29 of 38 = 0.76. The comparison of configurations is in [ADR 0017](../adr/0017-query-expansion-and-identifier-anchors.md).

## After the reference audit and a docs-aware judge (2026-10-18)

Two changes to the *measure*, not the system:

- **References:** 10 golden answers corrected where the docs contradict them ([reference-audit-v0](reference-audit-v0.md)).
- **Judge:** it now also sees the passages the answer cites, and follows them where they are more precise than the reference. It grades correctness only: a true statement the passages don't cover is not an error.

| Slice | n | Route | Context recall | Citation hit | Judge (1–5) | Correct |
|---|---|---|---|---|---|---|
| **overall** | 51 | 1.00 | 0.93 | 0.80 | 4.20 | **0.80** |
| complex | 15 | 1.00 | 0.93 | 0.87 | 4.33 | 0.87 |
| error_code | 11 | 1.00 | 0.91 | 0.55 | 3.91 | 0.64 |
| faq | 15 | 1.00 | 0.93 | 0.93 | 4.27 | 0.87 |

- **Bar:** 31 of 38 = **0.82** (≥ 0.85 needed), so still **not met**. No answer scores ≤ 2 and there are no invalid citations.
- **A misstep, kept for the record.** The first version of the new judge also counted any specific claim the cited passages didn't cover as an error. That pulled correctness down to 0.68, flagging true statements such as "In MongoDB 9.0" and `mongodump --uri`. That's groundedness, a separate metric (PLAN §11), so the rule was removed; this table is the only run after the fix. Judge prompts are not tuned further to move the number.
- **Comparability:** earlier rows on this page used the old judge and references. The 0.76 → 0.80 change is within the ±0.05 noise of 41 examples.
- **What's left:** all 8 examples below 4 are answers that leave out a point the reference makes and the docs support: the `mongodb+srv://` format, `validationAction`, the explicit-session fix for idle cursors, cache pressure, the ESR/ERS ordering, `authSource`, the standalone procedure, and indexing the foreign field for `$lookup`. Answer completeness is the next lever.

## Latency through the chat API (Phase 2 exit check)

`make eval-latency` sends 20 answerable golden questions one at a time through the running API (HTTP + SSE, as the widget does) after one warm-up request:

| | p50 | p90 | max |
|---|---|---|---|
| Time to first token | 1.36 s | **2.04 s** | 2.24 s |
| Full answer | 2.87 s | 4.00 s | 4.21 s |

The Phase 2 target, a warm first token under 3 s at P90, is met (2026-10-18, local Docker stack, `gpt-6-luna` with `reasoning_effort=low`).

## Choosing the answer model

Policy: start at the cheapest model and move up only when it fails the task's bar. **The bar was fixed before any run**: among examples where an expected page reached the model, ≥ 85 % score ≥ 4, none score ≤ 2, and no invalid citations.

| Run | Answer model | Prompt | Correct when context hit | Score ≤ 2 | TTFT p90 | Run cost |
|---|---|---|---|---|---|---|
| 1 | `gpt-6-luna` | v1 | 0.66 | 0 | 5.1 s | $0.40 |
| 2 | `gpt-6.1-sol` | v1 | 0.76 | 0 | 3.9 s | $1.09 |
| 3 | `gpt-6-luna` | v2 | 0.74 | 0 | 5.4 s | $0.44 |
| 4 | `gpt-6.1-sol` | v2 | 0.76 | 1 | 4.1 s | $1.18 |
| 5 | `gpt-6-luna`, effort `low` | v2 | 0.68 | 1 | **1.9 s** | $0.44 |

- **Prompt v2** replaced "be concise" with an instruction to state the prerequisites, defaults, limits and caveats found in the passages. Runs 1–2 showed both models dropping points that *were* in the context (`mongodb+srv://`, the replica-set requirement for change streams). The change is generic, not tuned to individual examples.
- **No model passed.** Luna and sol plateau around 0.75, and a model 20× the price did not move it. Reading the judge's reasons, the remaining failures are mostly not about the model:
  - **Reference answers less precise than the docs.** On `complex-write-concern-majority` the answer correctly says a majority write is acknowledged once it is in the majority's oplogs; the reference says "applied", and the judge marked the answer down. On `complex-transactions-standalone` the model repeated its passage's "multiple-node replica set"; the reference recommends a single-node replica set for development.
  - **Passages that never reach the model.** `error-cursor-not-found` expects "use an explicit session and refresh it"; that passage is not in the context.
  - **Genuine omissions** remain, e.g. `mongodb+srv://` skipped although it was in the context.
- **Decision (2026-10-18):** ship `gpt-6-luna` with `reasoning_effort=low`. It is the cheapest option and the only configuration that meets the < 3 s TTFT target. The bar is recorded as **not met**, and the model choice will be revisited once the follow-ups below land. `gpt-6-astra` was not run as an answer model.

## Follow-ups

1. **Golden answers:** review the reference answers the judge relied on where the docs are more precise (`complex-write-concern-majority`, `complex-transactions-standalone`), as a reviewed change.
2. **Judge against the docs:** give the judge the retrieved passages as well as the reference answer, so an answer that is correct per the docs is not penalized for differing from the reference wording.
3. **Retrieval for error codes** (context recall 0.64): error strings, Phase 3 graph expansion.
4. **Re-run the ladder** (luna → sol → astra) after 1–3.
