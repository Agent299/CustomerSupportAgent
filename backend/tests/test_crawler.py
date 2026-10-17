import httpx
import pytest

from shared.sources import DocVersion, doc_key, document_id, markdown_url, page_url
from workers.ingest.crawler import DocsCrawler, parse_sitemap

V9 = DocVersion("9.0", "manual")
V7 = DocVersion("7.0", "v7.0")

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.mongodb.com/docs/manual/</loc></url>
  <url><loc>https://www.mongodb.com/docs/manual/core/index-unique/</loc></url>
  <url><loc>https://www.mongodb.com/docs/manual/meta/hidden/</loc></url>
  <url><loc>https://www.mongodb.com/docs/atlas/elsewhere/</loc></url>
</urlset>"""

ROBOTS = "User-agent: *\nDisallow: /docs/manual/meta/\n"


def test_url_mapping_round_trip() -> None:
    assert doc_key("https://www.mongodb.com/docs/manual/core/index-unique/", V9) == (
        "core/index-unique"
    )
    assert doc_key("https://www.mongodb.com/docs/v7.0/", V7) == "index"
    assert markdown_url("core/index-unique", V7) == (
        "https://www.mongodb.com/docs/v7.0/core/index-unique.md"
    )
    assert markdown_url("index", V9) == "https://www.mongodb.com/docs/manual.md"
    assert page_url("core/index-unique", V9) == (
        "https://www.mongodb.com/docs/manual/core/index-unique/"
    )
    assert document_id("core/index-unique", V7) == "7.0:core/index-unique"


def test_doc_key_rejects_other_versions() -> None:
    with pytest.raises(ValueError):
        doc_key("https://www.mongodb.com/docs/v7.0/core/x/", V9)


def test_parse_sitemap() -> None:
    assert len(parse_sitemap(SITEMAP)) == 4


def test_parse_sitemap_rejects_entity_expansion() -> None:
    evil = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><urlset>&a;</urlset>'

    with pytest.raises(Exception, match=r"(?i)entit"):
        parse_sitemap(evil)


def _transport(routes: dict[str, httpx.Response]) -> httpx.MockTransport:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return routes.get(str(request.url), httpx.Response(404))

    transport = httpx.MockTransport(handler)
    transport.calls = calls  # type: ignore[attr-defined]
    return transport


async def test_discover_filters_robots_and_other_trees() -> None:
    transport = _transport(
        {
            "https://www.mongodb.com/robots.txt": httpx.Response(200, text=ROBOTS),
            V9.sitemap_url: httpx.Response(200, text=SITEMAP),
        }
    )
    async with DocsCrawler.make_client(transport) as client:
        crawler = DocsCrawler(client, rate_per_second=1000)
        await crawler.load_robots()

        keys = await crawler.discover(V9)

    assert keys == ["core/index-unique", "index"]


async def test_fetch_retries_transient_errors_then_succeeds() -> None:
    url = markdown_url("core/x", V9)
    responses = iter([httpx.Response(503), httpx.Response(200, text="# X\n")])
    transport = httpx.MockTransport(lambda request: next(responses))
    async with DocsCrawler.make_client(transport) as client:
        crawler = DocsCrawler(client, rate_per_second=1000, backoff_seconds=0)

        page = await crawler.fetch("core/x", V9)

    assert page.markdown == "# X\n"
    assert page.markdown_url == url


async def test_fetch_missing_page_returns_none() -> None:
    async with DocsCrawler.make_client(_transport({})) as client:
        page = await DocsCrawler(client, rate_per_second=1000).fetch("gone", V9)

    assert page.status == 404
    assert page.markdown is None
