"""Heading-aware Markdown chunker for MongoDB docs pages (docs/PLAN.md §8.5, ADR 0012).

Deterministic, no LLM:
1. normalize: drop the llms.txt banner; rewrite version-specific docs links to a
   ``{docs_version}`` placeholder and ``.md`` links to their HTML pages;
2. parse into blocks (headings / fenced code / text), ignoring ``#`` inside code fences;
3. group blocks into sections keyed by their heading breadcrumb;
4. pack each section into chunks of at most ``max_tokens``, never splitting a code block
   unless it alone exceeds the limit, with a small overlap between a section's chunks.

Chunk ids hash the breadcrumb + normalized text, so an identical section in several
versions yields one chunk (cross-version dedupe).
"""

import hashlib
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from functools import lru_cache

from shared.sources import DOCS_BASE

VERSION_PLACEHOLDER = "{docs_version}"
BREADCRUMB_SEP = " > "

TokenCounter = Callable[[str], int]

_BANNER = re.compile(r"^>\s*For the complete MongoDB documentation index.*$", re.MULTILINE)
_VERSIONED_MD_LINK = re.compile(
    re.escape(DOCS_BASE) + r"(?:manual|v\d+\.\d+)(?:/([^)\s#]*?))?\.md(#[^)\s]*)?"
)
_OTHER_MD_LINK = re.compile(re.escape(DOCS_BASE) + r"([^)\s#]+?)\.md(#[^)\s]*)?")
_INTERNAL_LINK = re.compile(
    re.escape(DOCS_BASE + VERSION_PLACEHOLDER) + r"/([^)\s#]*?)/?(?:#([^)\s]*))?(?=[)\s]|$)"
)
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_MD_LINK_TEXT = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class DocLink:
    """A link to another page of the same docs version (future REFERENCES edge)."""

    key: str
    anchor: str | None


@dataclass(frozen=True)
class Chunk:
    id: str
    ordinal: int
    breadcrumb: tuple[str, ...]
    text: str
    token_count: int
    links: tuple[DocLink, ...]

    @property
    def breadcrumb_text(self) -> str:
        return BREADCRUMB_SEP.join(self.breadcrumb)


@dataclass(frozen=True)
class ParsedPage:
    title: str
    chunks: list[Chunk]


@dataclass
class _Block:
    kind: str  # "heading" | "code" | "text"
    text: str
    level: int = 0


@dataclass
class _Section:
    path: tuple[str, ...]
    blocks: list[str] = field(default_factory=list)


@lru_cache(maxsize=1)
def _tiktoken_counter() -> TokenCounter:
    import tiktoken

    encoding = tiktoken.get_encoding("cl100k_base")  # tokenizer of text-embedding-3-*
    return lambda text: len(encoding.encode(text, disallowed_special=()))


def default_token_counter(text: str) -> int:
    return _tiktoken_counter()(text)


def normalize_markdown(markdown: str) -> str:
    text = _BANNER.sub("", markdown)

    def versioned(match: re.Match[str]) -> str:
        path, anchor = match.group(1), match.group(2) or ""
        suffix = f"{path}/" if path else ""
        return f"{DOCS_BASE}{VERSION_PLACEHOLDER}/{suffix}{anchor}"

    def other(match: re.Match[str]) -> str:
        return f"{DOCS_BASE}{match.group(1)}/{match.group(2) or ''}"

    text = _VERSIONED_MD_LINK.sub(versioned, text)
    text = _OTHER_MD_LINK.sub(other, text)
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(lines).strip() + "\n"


def extract_links(text: str) -> tuple[DocLink, ...]:
    seen: dict[tuple[str, str | None], DocLink] = {}
    for match in _INTERNAL_LINK.finditer(text):
        key = match.group(1).strip("/") or "index"
        link = DocLink(key=key, anchor=match.group(2) or None)
        seen.setdefault((link.key, link.anchor), link)
    return tuple(seen.values())


def _parse_blocks(markdown: str) -> Iterator[_Block]:
    buffer: list[str] = []
    fence: str | None = None

    def flush(kind: str) -> Iterator[_Block]:
        if buffer and any(line.strip() for line in buffer):
            yield _Block(kind, "\n".join(buffer).strip("\n"))
        buffer.clear()

    for line in markdown.splitlines():
        fence_match = _FENCE.match(line)
        if fence is not None:
            buffer.append(line)
            if (
                fence_match
                and fence_match.group(1)[0] == fence[0]
                and len(fence_match.group(1)) >= len(fence)
            ):
                fence = None
                yield from flush("code")
            continue
        if fence_match:
            yield from flush("text")
            fence = fence_match.group(1)
            buffer.append(line)
            continue
        heading = _HEADING.match(line)
        if heading:
            yield from flush("text")
            title = _MD_LINK_TEXT.sub(r"\1", heading.group(2)).strip()
            yield _Block("heading", title, level=len(heading.group(1)))
            continue
        if not line.strip():
            yield from flush("text")
            continue
        buffer.append(line)
    # An unterminated fence is still code.
    yield from flush("code" if fence else "text")


def _sections(blocks: Iterator[_Block]) -> tuple[str, list[_Section]]:
    title = ""
    stack: list[tuple[int, str]] = []
    sections = [_Section(path=())]
    for block in blocks:
        if block.kind == "heading":
            if block.level == 1 and not title:
                title = block.text
            while stack and stack[-1][0] >= block.level:
                stack.pop()
            stack.append((block.level, block.text))
            sections.append(_Section(path=tuple(text for _, text in stack)))
        else:
            sections[-1].blocks.append(block.text)
    if title and not sections[0].path:
        sections[0].path = (title,)
    return title, [s for s in sections if s.blocks]


def _split_oversized(block: str, max_tokens: int, count: TokenCounter) -> list[str]:
    """Split one block that alone exceeds ``max_tokens``; code and tables keep their framing."""
    lines = block.splitlines()
    if _FENCE.match(lines[0]):
        opener = lines[0]
        closer = lines[-1] if len(lines) > 1 and _FENCE.match(lines[-1]) else opener.strip()
        body = lines[1:-1] if closer == lines[-1] else lines[1:]
        return _pack_lines(body, max_tokens, count, head=[opener], tail=[closer])
    if lines[0].lstrip().startswith("|") and len(lines) > 2:
        return _pack_lines(lines[2:], max_tokens, count, head=lines[:2], tail=[])
    sentences = [s for s in _SENTENCE_END.split(block) if s]
    if len(sentences) == 1:
        words = block.split()
        return _pack_lines(words, max_tokens, count, head=[], tail=[], joiner=" ")
    return _pack_lines(sentences, max_tokens, count, head=[], tail=[], joiner=" ")


def _pack_lines(
    items: list[str],
    max_tokens: int,
    count: TokenCounter,
    *,
    head: list[str],
    tail: list[str],
    joiner: str = "\n",
) -> list[str]:
    def render(body: list[str]) -> str:
        return "\n".join([*head, joiner.join(body), *tail]) if head or tail else joiner.join(body)

    pieces: list[str] = []
    current: list[str] = []
    for item in items:
        if current and count(render([*current, item])) > max_tokens:
            pieces.append(render(current))
            current = []
        current.append(item)
    if current:
        pieces.append(render(current))
    return pieces


def _pack_section(
    blocks: list[str], max_tokens: int, overlap_tokens: int, count: TokenCounter
) -> list[str]:
    pieces: list[str] = []
    for block in blocks:
        if count(block) > max_tokens:
            pieces.extend(_split_oversized(block, max_tokens, count))
        else:
            pieces.append(block)

    chunks: list[list[str]] = []
    current: list[str] = []
    for piece in pieces:
        if current and count("\n\n".join([*current, piece])) > max_tokens:
            chunks.append(current)
            last = current[-1]
            overlap = [last] if count(last) <= overlap_tokens else []
            current = overlap if count("\n\n".join([*overlap, piece])) <= max_tokens else []
        current.append(piece)
    if current:
        chunks.append(current)
    return ["\n\n".join(chunk) for chunk in chunks]


def chunk_id(breadcrumb: tuple[str, ...], text: str) -> str:
    digest = hashlib.sha256(f"{BREADCRUMB_SEP.join(breadcrumb)}\n{text}".encode())
    return digest.hexdigest()[:16]


def chunk_markdown(
    markdown: str,
    *,
    fallback_title: str = "",
    max_tokens: int = 512,
    overlap_tokens: int = 64,
    min_section_tokens: int = 40,
    count_tokens: TokenCounter = default_token_counter,
) -> ParsedPage:
    normalized = normalize_markdown(markdown)
    title, sections = _sections(_parse_blocks(normalized))
    title = title or fallback_title

    chunks: list[Chunk] = []
    carried: list[str] = []  # a tiny intro is folded into its first subsection
    for index, section in enumerate(sections):
        blocks = [*carried, *section.blocks]
        carried = []
        next_path = sections[index + 1].path if index + 1 < len(sections) else ()
        is_parent_of_next = len(next_path) > len(section.path) and (
            next_path[: len(section.path)] == section.path
        )
        if is_parent_of_next and count_tokens("\n\n".join(blocks)) < min_section_tokens:
            carried = blocks
            continue
        path = section.path or ((title,) if title else ())
        for text in _pack_section(blocks, max_tokens, overlap_tokens, count_tokens):
            chunks.append(
                Chunk(
                    id=chunk_id(path, text),
                    ordinal=len(chunks),
                    breadcrumb=path,
                    text=text,
                    token_count=count_tokens(text),
                    links=extract_links(text),
                )
            )
    return ParsedPage(title=title, chunks=chunks)
