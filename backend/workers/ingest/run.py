"""Ingestion entry point.

    uv run python -m workers.ingest.run all --limit 20       # whole pipeline, then exit
    uv run python -m workers.ingest.run schedule              # emit raw_documents only
    uv run python -m workers.ingest.run chunk-worker --follow # consume raw_documents
    uv run python -m workers.ingest.run index-worker --follow # consume chunked_documents
    uv run python -m workers.ingest.run reindex               # embed anything not yet indexed
    uv run python -m workers.ingest.run relay --interval 60   # publish webhook outbox rows
    uv run python -m workers.ingest.run gc                    # drop orphan chunks (PG + index)

``all`` drains each stage in turn and exits (local runs, one-off backfills). In production
(deploy/oracle) the workers and relay run continuously, and a nightly timer runs ``schedule``
then ``gc``.
"""

import argparse
import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import func, update

from app.config import Settings, get_settings
from app.genai.embeddings import Embedder, make_embedder
from app.retrieval import kb_index
from shared.bus import EventBus, InMemoryBus, KafkaBus
from shared.db.models import IngestRun
from shared.db.session import make_engine, make_session_factory
from shared.events import ALL_TOPICS, TOPIC_CHUNKED_DOCUMENTS, TOPIC_RAW_DOCUMENTS
from shared.logging import configure_logging
from shared.sources import DEFAULT_VERSIONS, DocVersion, get_version
from workers.ingest.crawler import DocsCrawler
from workers.ingest.pipeline import CHUNK_GROUP, INDEX_GROUP, Pipeline

COMMANDS = ("all", "schedule", "chunk-worker", "index-worker", "reindex", "relay", "gc")


@asynccontextmanager
async def build_pipeline(
    settings: Settings,
    versions: tuple[DocVersion, ...],
    *,
    bus: EventBus | None = None,
    crawler: DocsCrawler | None = None,
    embedder: Embedder | None = None,
    rate_per_second: float = 2.0,
) -> AsyncIterator[Pipeline]:
    engine = make_engine(settings.database_url)
    search = kb_index.make_client(settings)
    if bus is None:
        kafka = KafkaBus(settings.kafka_bootstrap_servers)
        await kafka.ensure_topics(ALL_TOPICS)
        bus = kafka
    index = kb_index.KbIndex(search, settings.opensearch_index, settings.embedding_dimensions)
    await index.ensure()
    async with DocsCrawler.make_client() as http:
        try:
            yield Pipeline(
                bus=bus,
                sessions=make_session_factory(engine),
                crawler=crawler or DocsCrawler(http, rate_per_second=rate_per_second),
                index=index,
                embedder=embedder or make_embedder(settings),
                versions=versions,
            )
        finally:
            await bus.close()
            await search.close()
            await engine.dispose()


async def run_all(pipeline: Pipeline, *, limit: int | None, idle_timeout_s: float) -> None:
    """Schedule, then drain each stage in order, then GC. Recorded in ``ingest_runs``."""
    async with pipeline.sessions.begin() as session:
        run_row = IngestRun(
            stats={"versions": [v.label for v in pipeline.versions], "limit": limit}
        )
        session.add(run_row)
    status = "failed"
    try:
        await pipeline.schedule(limit=limit)
        await pipeline.bus.consume(
            TOPIC_RAW_DOCUMENTS, CHUNK_GROUP, pipeline.handle_raw, idle_timeout_s=idle_timeout_s
        )
        await pipeline.bus.consume(
            TOPIC_CHUNKED_DOCUMENTS,
            INDEX_GROUP,
            pipeline.handle_chunked,
            idle_timeout_s=idle_timeout_s,
        )
        await pipeline.collect_garbage()
        status = "success"
    finally:
        async with pipeline.sessions.begin() as session:
            await session.execute(
                update(IngestRun)
                .where(IngestRun.id == run_row.id)
                .values(
                    status=status,
                    finished_at=func.now(),
                    stats=run_row.stats | {"counts": dict(pipeline.stats)},
                )
            )


async def main_async(args: argparse.Namespace) -> dict[str, int]:
    settings = get_settings()
    versions = tuple(get_version(label) for label in args.versions)
    bus: EventBus | None = InMemoryBus() if args.bus == "memory" else None
    idle = None if args.follow else args.idle_timeout
    async with build_pipeline(settings, versions, bus=bus, rate_per_second=args.rate) as pipeline:
        if args.command == "all":
            await run_all(pipeline, limit=args.limit, idle_timeout_s=args.idle_timeout)
        elif args.command == "schedule":
            await pipeline.schedule(limit=args.limit)
        elif args.command == "chunk-worker":
            await pipeline.bus.consume(
                TOPIC_RAW_DOCUMENTS, CHUNK_GROUP, pipeline.handle_raw, idle_timeout_s=idle
            )
        elif args.command == "index-worker":
            await pipeline.bus.consume(
                TOPIC_CHUNKED_DOCUMENTS, INDEX_GROUP, pipeline.handle_chunked, idle_timeout_s=idle
            )
        elif args.command == "reindex":
            await pipeline.index_pending()
        elif args.command == "relay":
            while True:
                await pipeline.drain_outbox()
                await asyncio.sleep(args.interval)
        elif args.command == "gc":
            await pipeline.collect_garbage()
        return dict(pipeline.stats)


def main() -> None:
    parser = argparse.ArgumentParser(description="MongoDB docs ingestion")
    parser.add_argument("command", nargs="?", default="all", choices=COMMANDS)
    parser.add_argument(
        "--versions", nargs="+", default=[v.label for v in DEFAULT_VERSIONS], metavar="LABEL"
    )
    parser.add_argument("--limit", type=int, help="max pages per version (smoke runs)")
    parser.add_argument("--rate", type=float, default=2.0, help="max docs requests per second")
    parser.add_argument("--follow", action="store_true", help="workers: run until stopped")
    parser.add_argument("--interval", type=float, default=60.0, help="relay: seconds between polls")
    parser.add_argument(
        "--idle-timeout", type=float, default=15.0, help="seconds of no messages before exiting"
    )
    parser.add_argument(
        "--bus", choices=["kafka", "memory"], default="kafka", help="memory: no Redpanda needed"
    )
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level, json=settings.app_env != "local")
    stats = asyncio.run(main_async(args))
    print(json.dumps(stats, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
