# Reference audit — golden set v0 (2026-10-18)

All 41 answerable references were checked against their expected source pages, not just the ones the system failed on.

1. **Flagging:** `gpt-6-astra` got each reference answer with the most relevant passages from its expected pages, and listed statements the docs contradict or state materially differently, each with a verbatim doc quote. Omissions and style were out of scope. 19 of 41 were flagged (cost $1.13).
2. **Quote check:** each of the 26 quoted doc sentences was matched against the stored page text. 25 matched exactly; the other (`complex-unique-sharded`) was found once Markdown link syntax was set aside.
3. **Decision rule:** a reference changes only where the docs contradict it or state a condition differently, so that an answer following the docs would be marked down. Caveats the reference doesn't mention, and nitpicks, don't count. Key facts were not changed (`make eval-validate`: 0 problems).

| Example | Decision | Why |
|---|---|---|
| `complex-find-sort-slow` | Corrected | The docs make sort-before-range (ESR) or range-before-sort (ERS) a choice; the reference made ESR a rule. |
| `complex-read-preference` | Corrected | The docs say primaryPreferred may return stale data; the reference recommended it for current reads. |
| `complex-retryable-writes` | Corrected | One retry is the default (timeoutMS can allow more). |
| `complex-shard-key` | Corrected | Monotonically increasing *or decreasing* values concentrate inserts. |
| `complex-transactions-standalone` | Corrected | The docs require a multiple-node replica set; the reference recommended a single-node one. |
| `complex-write-concern-majority` | Corrected | Since 8.0, acknowledgment follows the durable oplog write; members apply asynchronously. The reference said “applied”. |
| `error-cursor-not-found` | Corrected | The explicit-session advice applies when the cursor may be idle over 30 minutes, not to long jobs in general. |
| `error-max-time` | Corrected | The limit can come from defaultMaxTimeMS when the operation sets no maxTimeMS. |
| `error-write-conflict` | Corrected | Excessive cache pressure aborts with a write conflict; a transaction too large for the cache is a different error. |
| `faq-enable-auth` | Corrected | The docs scope that procedure to a standalone mongod; replica sets and sharded clusters have their own. |
| `complex-embed-vs-reference` | Kept | A paraphrase of the docs' criteria, not a contradiction. |
| `complex-unique-sharded` | Kept | The docs scope the list to ranged sharding; that doesn't change the answer to this question. |
| `error-auth-failed` | Kept | Listing which characters need encoding is more detail, not a contradiction. |
| `error-e11000` | Kept | Index builds can also raise E11000; the reference's write case is still correct. |
| `error-e11000-multiturn` | Kept | Same as error-e11000. |
| `faq-backup-tools` | Kept | The docs also say the tools suit small deployments; the reference paraphrases faithfully. |
| `faq-change-streams` | Kept | Scope limits (system collections) and durability wording are caveats, not contradictions. |
| `faq-delete-many` | Kept | A caveat for sharded collections during chunk migrations; not a contradiction. |
| `faq-update-set` | Kept | Type-constraint edge case; an answer that follows the docs isn't marked down by the reference. |

Result: **10 corrected**, 9 kept. The 22 unflagged references were not changed.
