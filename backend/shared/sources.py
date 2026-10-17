"""MongoDB docs source definitions and URL mapping.

The server manual is published per version under https://www.mongodb.com/docs/<path>/,
where ``manual`` is the current release. Every HTML page has an official Markdown twin
at ``<page-without-trailing-slash>.md``, which is what we ingest (see docs/PLAN.md §8.5).
"""

import re
from dataclasses import dataclass

DOCS_BASE = "https://www.mongodb.com/docs/"


@dataclass(frozen=True)
class DocVersion:
    label: str  # what users say: "9.0"
    path: str  # URL segment: "manual" or "v8.0"

    @property
    def root_url(self) -> str:
        return f"{DOCS_BASE}{self.path}/"

    @property
    def sitemap_url(self) -> str:
        return f"{self.root_url}sitemap-0.xml"


# Current release + previous major releases (8.3 is an Atlas-only rapid release).
DEFAULT_VERSIONS: tuple[DocVersion, ...] = (
    DocVersion("9.0", "manual"),
    DocVersion("8.0", "v8.0"),
    DocVersion("7.0", "v7.0"),
)

VERSION_PATH_RE = re.compile(r"^(manual|v\d+\.\d+)$")


def get_version(label: str, versions: tuple[DocVersion, ...] = DEFAULT_VERSIONS) -> DocVersion:
    for version in versions:
        if version.label == label:
            return version
    known = ", ".join(v.label for v in versions)
    raise ValueError(f"Unknown docs version {label!r}; known: {known}")


def doc_key(page_url: str, version: DocVersion) -> str:
    """Version-independent page key, e.g. ``core/index-unique`` (root page → ``index``).

    The same key across versions is what lets us link and dedupe a page's versions.
    """
    if not page_url.startswith(version.root_url.rstrip("/")):
        raise ValueError(f"{page_url} is not under {version.root_url}")
    rest = page_url[len(version.root_url.rstrip("/")) :].strip("/")
    rest = rest.split("#", 1)[0].split("?", 1)[0]
    rest = rest.removesuffix(".md")
    return rest or "index"


def markdown_url(key: str, version: DocVersion) -> str:
    """Official Markdown URL for a page key."""
    if key == "index":
        return f"{DOCS_BASE}{version.path}.md"
    return f"{version.root_url}{key}.md"


def page_url(key: str, version: DocVersion) -> str:
    """Human-facing HTML URL for a page key (used in citations)."""
    if key == "index":
        return version.root_url
    return f"{version.root_url}{key}/"


def document_id(key: str, version: DocVersion) -> str:
    return f"{version.label}:{key}"
