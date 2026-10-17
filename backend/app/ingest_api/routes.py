"""POST /api/ingest/collect — source webhooks (docs/PLAN.md §8.2).

Writes to the ``ingest_outbox`` table; the next ingestion run publishes it to
``raw_documents``. The API therefore never needs Kafka.
"""

import secrets
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, HttpUrl
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from shared.db.models import IngestOutbox
from shared.events import RawDocumentEvent
from shared.sources import DEFAULT_VERSIONS, doc_key

router = APIRouter(prefix="/api/ingest", tags=["ingest"])


class CollectRequest(BaseModel):
    source_id: str
    url: HttpUrl
    type: Literal["DOC", "TICKET"]
    op: Literal["UPSERT", "DELETE"] = "UPSERT"


class CollectResponse(BaseModel):
    status: Literal["QUEUED"] = "QUEUED"
    message: str = "Source item added to raw_documents buffer."
    outbox_id: int


def require_ingest_token(
    request: Request, x_ingest_token: Annotated[str | None, Header()] = None
) -> None:
    expected = request.app.state.settings.ingest_webhook_token
    if expected is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)  # endpoint disabled
    if x_ingest_token is None or not secrets.compare_digest(
        x_ingest_token, expected.get_secret_value()
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid ingest token")


@router.post(
    "/collect",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_ingest_token)],
)
async def collect(
    body: CollectRequest, session: Annotated[AsyncSession, Depends(get_session)]
) -> CollectResponse:
    if body.type == "TICKET":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "TICKET sources are ingested from Phase 4"
        )
    url = str(body.url)
    for version in DEFAULT_VERSIONS:
        if url.startswith(version.root_url) or url.rstrip("/") == version.root_url.rstrip("/"):
            event = RawDocumentEvent(version=version.label, key=doc_key(url, version), op=body.op)
            row = IngestOutbox(payload=event.model_dump(mode="json"))
            session.add(row)
            await session.flush()
            return CollectResponse(outbox_id=row.id)
    labels = ", ".join(v.label for v in DEFAULT_VERSIONS)
    raise HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        f"URL is not under an indexed docs version ({labels})",
    )
