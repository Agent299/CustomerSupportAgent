from workers.ingest.chunker import (
    VERSION_PLACEHOLDER,
    DocLink,
    chunk_markdown,
    extract_links,
    normalize_markdown,
)


def words(text: str) -> int:
    """Deterministic stand-in for tiktoken in unit tests."""
    return len(text.split())


PAGE = """> For the complete MongoDB documentation index, see www.mongodb.com/docs/llms.txt

# Widgets

Widgets store things. See [indexes](https://www.mongodb.com/docs/manual/indexes.md#std-label-indexes) for details.

## Create a Widget

Use the method below to create a widget in a collection safely.

```javascript
# this is a comment, not a heading
db.widgets.insertOne( { name: "w" } )
```

## Behavior

### Restrictions

You cannot create two widgets with the same name in one collection, ever.
"""


def test_banner_removed_and_links_normalized() -> None:
    text = normalize_markdown(PAGE)

    assert "llms.txt" not in text
    assert f"https://www.mongodb.com/docs/{VERSION_PLACEHOLDER}/indexes/#std-label-indexes" in text


def test_versioned_links_normalize_identically() -> None:
    v9 = "[x](https://www.mongodb.com/docs/manual/core/document.md#a)"
    v7 = "[x](https://www.mongodb.com/docs/v7.0/core/document.md#a)"

    assert normalize_markdown(v9) == normalize_markdown(v7)


def test_other_product_links_point_to_html_without_placeholder() -> None:
    text = normalize_markdown("[shell](https://www.mongodb.com/docs/mongodb-shell.md#x)")

    assert "https://www.mongodb.com/docs/mongodb-shell/#x" in text


def test_extract_links_returns_internal_page_keys() -> None:
    text = normalize_markdown(
        "[a](https://www.mongodb.com/docs/manual/core/document.md#id) "
        "[root](https://www.mongodb.com/docs/manual.md) "
        "[a again](https://www.mongodb.com/docs/manual/core/document.md#id)"
    )

    assert extract_links(text) == (
        DocLink(key="core/document", anchor="id"),
        DocLink(key="index", anchor=None),
    )


def test_sections_get_breadcrumbs_and_code_fence_hash_is_not_a_heading() -> None:
    page = chunk_markdown(PAGE, count_tokens=words, min_section_tokens=0)

    assert page.title == "Widgets"
    assert [c.breadcrumb for c in page.chunks] == [
        ("Widgets",),
        ("Widgets", "Create a Widget"),
        ("Widgets", "Behavior", "Restrictions"),
    ]
    create = page.chunks[1]
    assert "# this is a comment, not a heading" in create.text
    assert create.text.rstrip().endswith("```")


def test_tiny_intro_is_folded_into_first_subsection() -> None:
    md = "# T\n\n## Parent\n\nShort intro.\n\n### Child\n\nThe child section has its own body text.\n"

    page = chunk_markdown(md, count_tokens=words, min_section_tokens=5)

    assert len(page.chunks) == 1
    assert page.chunks[0].breadcrumb == ("T", "Parent", "Child")
    assert page.chunks[0].text.startswith("Short intro.")


def test_long_section_splits_within_limit_with_overlap() -> None:
    paragraphs = "\n\n".join(f"Paragraph {i} " + "word " * 20 for i in range(10))
    md = f"# T\n\n## Long\n\n{paragraphs}\n"

    chunks = chunk_markdown(md, count_tokens=words, max_tokens=60, overlap_tokens=30).chunks

    assert len(chunks) > 1
    assert all(c.token_count <= 60 for c in chunks)
    # The last paragraph of one chunk opens the next one.
    first_last_para = chunks[0].text.split("\n\n")[-1]
    assert chunks[1].text.startswith(first_last_para)


def test_oversized_code_block_is_split_but_stays_fenced() -> None:
    code = "\n".join(f"line_{i} = {i}" for i in range(100))
    md = f"# T\n\n## Code\n\n```python\n{code}\n```\n"

    chunks = chunk_markdown(md, count_tokens=words, max_tokens=50).chunks

    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.text.startswith("```python")
        assert chunk.text.endswith("```")


def test_oversized_table_repeats_header() -> None:
    rows = "\n".join(f"| E{i} | Error number {i} |" for i in range(60))
    md = f"# T\n\n## Codes\n\n| Code | Name |\n| --- | --- |\n{rows}\n"

    chunks = chunk_markdown(md, count_tokens=words, max_tokens=80).chunks

    assert len(chunks) > 1
    assert all(c.text.startswith("| Code | Name |\n| --- | --- |") for c in chunks)


def test_identical_sections_across_versions_share_chunk_ids() -> None:
    v9 = PAGE
    v7 = PAGE.replace("/docs/manual/", "/docs/v7.0/")

    ids_v9 = [c.id for c in chunk_markdown(v9, count_tokens=words).chunks]
    ids_v7 = [c.id for c in chunk_markdown(v7, count_tokens=words).chunks]

    assert ids_v9 == ids_v7


def test_changed_text_changes_chunk_id() -> None:
    before = chunk_markdown(PAGE, count_tokens=words).chunks
    after = chunk_markdown(PAGE.replace("ever.", "at all."), count_tokens=words).chunks

    assert before[-1].id != after[-1].id
    assert before[0].id == after[0].id
