"""Sitemap discovery and polite fetching of MongoDB docs Markdown pages.

Politeness: identifies itself, honours robots.txt, caps request rate (default 2 req/s),
retries transient failures with backoff. The sitemaps carry no <lastmod>, and the .md
pages no ETag, so change detection happens downstream by content hash.
"""

import asyncio
import time
from dataclasses import dataclass
from urllib.robotparser import RobotFileParser

import httpx
from defusedxml import ElementTree

from shared.sources import DOCS_BASE, DocVersion, doc_key, markdown_url

USER_AGENT = "SupportAgentPortfolioBot/0.1 (+https://github.com/Agent299/CustomerSupportAgent)"
ROBOTS_URL = "https://www.mongodb.com/robots.txt"
_SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
_RETRY_STATUSES = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class FetchedPage:
    key: str
    version: DocVersion
    markdown_url: str
    status: int
    markdown: str | None  # None when the page is gone (404/410)


def parse_sitemap(xml: str) -> list[str]:
    root = ElementTree.fromstring(xml)
    return [
        loc.text.strip() for loc in root.iter(f"{_SITEMAP_NS}loc") if loc.text and loc.text.strip()
    ]


class RateLimiter:
    """Spaces request starts at least ``1 / rate`` seconds apart across all tasks."""

    def __init__(self, rate_per_second: float) -> None:
        self._interval = 1.0 / rate_per_second
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self._interval
        if delay > 0:
            await asyncio.sleep(delay)


class DocsCrawler:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        rate_per_second: float = 2.0,
        max_retries: int = 3,
        backoff_seconds: float = 1.0,
    ) -> None:
        self._client = client
        self._limiter = RateLimiter(rate_per_second)
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._robots: RobotFileParser | None = None

    @staticmethod
    def make_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            transport=transport,
        )

    async def _get(self, url: str) -> httpx.Response:
        for attempt in range(self._max_retries + 1):
            await self._limiter.wait()
            try:
                response = await self._client.get(url)
            except httpx.TransportError:
                if attempt == self._max_retries:
                    raise
            else:
                if response.status_code not in _RETRY_STATUSES or attempt == self._max_retries:
                    return response
            await asyncio.sleep(self._backoff * 2**attempt)
        raise AssertionError("unreachable")

    async def load_robots(self) -> None:
        response = await self._get(ROBOTS_URL)
        parser = RobotFileParser(ROBOTS_URL)
        parser.parse(response.text.splitlines() if response.status_code == 200 else [])
        self._robots = parser

    def allowed(self, url: str) -> bool:
        return self._robots is None or self._robots.can_fetch(USER_AGENT, url)

    async def discover(self, version: DocVersion) -> list[str]:
        """Page keys listed in the version's sitemap, filtered by robots.txt."""
        response = await self._get(version.sitemap_url)
        response.raise_for_status()
        keys: list[str] = []
        for url in parse_sitemap(response.text):
            if not url.startswith(DOCS_BASE) or not self.allowed(url):
                continue
            try:
                keys.append(doc_key(url, version))
            except ValueError:
                continue  # sitemap entry outside this version's tree
        return sorted(set(keys))

    async def fetch(self, key: str, version: DocVersion) -> FetchedPage:
        url = markdown_url(key, version)
        response = await self._get(url)
        if response.status_code in (404, 410):
            return FetchedPage(key, version, url, response.status_code, None)
        response.raise_for_status()
        return FetchedPage(key, version, url, response.status_code, response.text)
