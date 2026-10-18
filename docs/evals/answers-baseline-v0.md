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
