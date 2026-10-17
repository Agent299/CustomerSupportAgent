# Architecture Decision Records

| ADR | Title |
|---|---|
| [0001](0001-python-workers-instead-of-spark.md) | Python workers instead of Spark/Glue; keep topic contracts |
| [0002](0002-modular-monolith-core-api.md) | Modular monolith for the core API |
| [0003](0003-hybrid-search-opensearch.md) | Hybrid BM25 + kNN search in OpenSearch |
| [0004](0004-embed-chunk-text.md) | Embed chunk text with breadcrumb and entities |
| [0005](0005-chunk-text-in-postgres.md) | Chunk text in Postgres; object storage for raw snapshots only |
| [0006](0006-entity-extraction-rules-plus-llm.md) | Entity extraction via rules plus the cheapest capable LLM |
| [0007](0007-calibrated-confidence.md) | Calibrated 0–1 confidence score |
| [0008](0008-tickets-feed-knowledge-base.md) | Human-approved resolved tickets feed the knowledge base |
| [0009](0009-thin-genai-client.md) | Thin in-house GenAI client on the official OpenAI SDK |
| [0010](0010-latency-budgets.md) | Explicit latency budgets |
| [0011](0011-llm-usage-policy.md) | Deterministic first; cheapest capable model; frontier only for complex tasks |
| [0012](0012-version-aware-dedup.md) | Version-aware, content-hash-deduplicated chunks |
| [0013](0013-email-code-auth.md) | Passwordless email-code authentication, built in-house |
| [0014](0014-render-and-free-datastores.md) | ~~Render for compute; free managed services for state~~ (superseded by 0016) |
| [0015](0015-official-markdown-source.md) | Ingest MongoDB's official Markdown pages, not HTML |
| [0016](0016-oracle-cloud-single-vm.md) | Host everything on one Oracle Cloud Always Free VM |

See [`../PLAN.md`](../PLAN.md) §4 for the summary table.
