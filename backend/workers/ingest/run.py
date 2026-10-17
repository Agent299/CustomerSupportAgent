"""Ingestion run: discover → fetch → redact → chunk → store, per docs version.

    uv run python -m workers.ingest.run --versions 9.0 --limit 50

Re-running is a no-op for unchanged pages (content-hash check). Pages that disappear
from a sitemap are marked deleted, but only on full (un-limited) runs.
"""

import argparse
import asyncio
import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field

import structlog
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.privacy.redact import Profile, redact
from shared.db.models import IngestRun
from shared.db.session import make_engine, make_session_factory
from shared.logging import configure_logging
from shared.sources import DEFAULT_VERSIONS, DocVersion, get_version
from workers.ingest.chunker import TokenCounter, chunk_markdown, default_token_counter
from workers.ingest.crawler import DocsCrawler, FetchedPage
from workers.ingest.store import content_hashes, delete_orphan_chunks, mark_deleted, upsert_page

log = structlog.get_logger()


@dataclass
class RunStats:
    pages: Counter[str] = field(default_factory=Counter)
    new_chunk_ids: set[str] = field(default_factory=set)
    orphan_chunks_deleted: int = 0

    def add(self, version: DocVersion, outcome: str, n: int = 1) -> None:
        if n:
            self.pages[f"{version.label}:{outcome}"] += n

    def as_dict(self) -> dict[str, object]:
        return {
            "pages": dict(self.pages),
            "new_chunks": len(self.new_chunk_ids),
            "orphan_chunks_deleted": self.orphan_chunks_deleted,
        }


def page_hash(markdown: str) -> str:
    return hashlib.sha256(markdown.encode()).hexdigest()


async def _process(
    page: FetchedPage,
    known_hashes: dict[str, str],
    sessions: async_sessionmaker[AsyncSession],
    count_tokens: TokenCounter,
    stats: RunStats,
) -> None:
    if page.markdown is None:
        async with sessions.begin() as session:
            stats.add(page.version, "gone", await mark_deleted(session, page.version, [page.key]))
        return
    markdown = redact(page.markdown, Profile.SECRETS)
    digest = page_hash(markdown)
    if known_hashes.get(page.key) == digest:
        stats.add(page.version, "unchanged")
        return
    parsed = await asyncio.to_thread(
        chunk_markdown, markdown, fallback_title=page.key, count_tokens=count_tokens
    )
    async with sessions.begin() as session:
        written = await upsert_page(
            session, key=page.key, version=page.version, parsed=parsed, content_hash=digest
        )
    stats.new_chunk_ids.update(written.new_chunk_ids)
    stats.add(page.version, "updated" if page.key in known_hashes else "added")


async def ingest_version(
    crawler: DocsCrawler,
    sessions: async_sessionmaker[AsyncSession],
    version: DocVersion,
    stats: RunStats,
    *,
    limit: int | None = None,
    concurrency: int = 4,
    count_tokens: TokenCounter = default_token_counter,
) -> None:
    keys = await crawler.discover(version)
    async with sessions() as session:
        known = await content_hashes(session, version)
    if limit is None:
        removed = sorted(set(known) - set(keys))
        async with sessions.begin() as session:
            stats.add(version, "removed", await mark_deleted(session, version, removed))
    else:
        keys = keys[:limit]
    log.info("ingest.version.start", version=version.label, pages=len(keys))

    semaphore = asyncio.Semaphore(concurrency)

    async def one(key: str) -> None:
        async with semaphore:
            try:
                page = await crawler.fetch(key, version)
                await _process(page, known, sessions, count_tokens, stats)
            except Exception:
                stats.add(version, "failed")
                log.exception("ingest.page.failed", version=version.label, key=key)

    await asyncio.gather(*(one(key) for key in keys))
    log.info("ingest.version.done", version=version.label, stats=dict(stats.pages))


async def run(
    versions: tuple[DocVersion, ...],
    *,
    database_url: str,
    limit: int | None = None,
    rate_per_second: float = 2.0,
    crawler: DocsCrawler | None = None,
    count_tokens: TokenCounter = default_token_counter,
) -> RunStats:
    engine = make_engine(database_url)
    sessions = make_session_factory(engine)
    stats = RunStats()
    async with sessions.begin() as session:
        run_row = IngestRun(stats={"versions": [v.label for v in versions], "limit": limit})
        session.add(run_row)
    status = "failed"
    try:
        if crawler is None:
            async with DocsCrawler.make_client() as client:
                await _ingest_all(
                    DocsCrawler(client, rate_per_second=rate_per_second),
                    sessions,
                    versions,
                    stats,
                    limit,
                    count_tokens,
                )
        else:
            await _ingest_all(crawler, sessions, versions, stats, limit, count_tokens)
        async with sessions.begin() as session:
            stats.orphan_chunks_deleted = len(await delete_orphan_chunks(session))
        failed = any(key.endswith(":failed") for key in stats.pages)
        status = "partial" if failed else "success"
        return stats
    finally:
        async with sessions.begin() as session:
            await session.execute(
                update(IngestRun)
                .where(IngestRun.id == run_row.id)
                .values(
                    status=status, finished_at=func.now(), stats=run_row.stats | stats.as_dict()
                )
            )
        await engine.dispose()


async def _ingest_all(
    crawler: DocsCrawler,
    sessions: async_sessionmaker[AsyncSession],
    versions: tuple[DocVersion, ...],
    stats: RunStats,
    limit: int | None,
    count_tokens: TokenCounter,
) -> None:
    await crawler.load_robots()
    for version in versions:
        await ingest_version(
            crawler, sessions, version, stats, limit=limit, count_tokens=count_tokens
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--versions", nargs="+", default=[v.label for v in DEFAULT_VERSIONS], metavar="LABEL"
    )
    parser.add_argument("--limit", type=int, help="max pages per version (smoke runs)")
    parser.add_argument("--rate", type=float, default=2.0, help="max requests per second")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level, json=settings.app_env != "local")
    versions = tuple(get_version(label) for label in args.versions)
    stats = asyncio.run(
        run(
            versions,
            database_url=settings.database_url,
            limit=args.limit,
            rate_per_second=args.rate,
        )
    )
    print(json.dumps(stats.as_dict(), indent=2))


if __name__ == "__main__":
    main()
