"""MongoDB identifiers in text: operators, methods, API names, error names and codes.

Shared by ingestion, which builds the anchor index from docs pages, and the query path,
which looks up the identifiers a question mentions (docs/research/graphrag-retrieval-study.md).
"""

import re

_IDENTIFIER = re.compile(
    r"\$[a-zA-Z]\w+"  # $median, $group
    r"|\b[a-z]+(?:\.[a-zA-Z]+)*\.[a-z]+[A-Za-z]*\(\)"  # db.collection.insertOne(), cursor.sort()
    r"|\b[a-z]+[A-Z][A-Za-z0-9]*\b"  # camelCase: maxTimeMS, noCursorTimeout, insertOne
    r"|\b[A-Z][A-Za-z0-9]*[a-z][A-Z][A-Za-z0-9]*\b"  # PascalCase: DuplicateKey, CursorNotFound
    r"|\b[A-Z]{2,}[a-z]\w*[A-Z]\w*\b"  # BSONObjectTooLarge
    r"|\bE\d{4,5}\b"  # E11000
)


def anchor_terms(text: str) -> set[str]:
    """Normalized identifiers: lowercase, call parentheses and receiver dropped
    (``cursor.sort()`` → ``sort``); shorter than 4 characters ignored."""
    found: set[str] = set()
    for match in _IDENTIFIER.findall(text):
        token = match.rstrip("()").lower()
        token = token.rsplit(".", 1)[-1] if "." in token else token
        if len(token) >= 4:
            found.add(token)
    return found
