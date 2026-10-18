"""Query-time lookup in the identifier anchor index (built by workers/ingest/anchors.py).

A question that names ``NotWritablePrimary`` or ``$median`` gets the pages that define or
centre on it, even when hybrid search ranks generic pages higher.
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shared.db.models import IdentifierAnchor
from shared.identifiers import anchor_terms

MAX_ANCHOR_PAGES = 8


async def anchor_pages(
    sessions: async_sessionmaker[AsyncSession], version: str, text: str
) -> list[str]:
    """Pages anchored by the identifiers in ``text``, best first; [] when it names none."""
    terms = sorted(anchor_terms(text))
    if not terms:
        return []
    score = func.sum(IdentifierAnchor.score)
    async with sessions() as session:
        rows = await session.execute(
            select(IdentifierAnchor.doc_key, score)
            .where(IdentifierAnchor.version == version, IdentifierAnchor.identifier.in_(terms))
            .group_by(IdentifierAnchor.doc_key)
            .order_by(score.desc(), IdentifierAnchor.doc_key)
            .limit(MAX_ANCHOR_PAGES)
        )
    return [doc_key for doc_key, _ in rows]
