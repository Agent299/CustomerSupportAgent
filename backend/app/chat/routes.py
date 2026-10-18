"""Customer chat API (docs/PLAN.md §8.2).

Until Phase 4 adds login, a session belongs to whoever holds its token, sent in the
``X-Session-Token`` header (never in a URL, so it stays out of access logs). Someone else's
session answers 404, not 403. The widget reads the stream with fetch(), which can send the
header; the browser's EventSource cannot.
"""

import asyncio
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.chat.service import ChatService, TurnInProgress
from app.orchestrator.rules import SUPPORTED_VERSIONS

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])

Version = Literal[SUPPORTED_VERSIONS]  # type: ignore[valid-type]
Token = Annotated[str, Header(alias="X-Session-Token", min_length=20, max_length=200)]


def get_chat(request: Request) -> ChatService:
    chat: ChatService | None = request.app.state.chat
    if chat is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Chat is not configured")
    return chat


Chat = Annotated[ChatService, Depends(get_chat)]


class CreateSession(BaseModel):
    version_hint: Version | None = None


class SessionCreated(BaseModel):
    session_id: uuid.UUID
    session_token: str


class PostMessage(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    version_hint: Version | None = None


class MessageAccepted(BaseModel):
    message_id: uuid.UUID
    stream_after: str  # pass as ?after= to the stream to receive exactly this turn onward


class TranscriptMessage(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    citations: list[dict[str, Any]]
    route: str | None
    created_at: datetime


class Transcript(BaseModel):
    session_id: uuid.UUID
    status: str
    messages: list[TranscriptMessage]


class FeedbackIn(BaseModel):
    rating: Literal[1, -1]
    comment: str | None = Field(default=None, max_length=2000)


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
async def create_session(chat: Chat, body: CreateSession | None = None) -> SessionCreated:
    session_id, token = await chat.create_session(body.version_hint if body else None)
    return SessionCreated(session_id=session_id, session_token=token)


@router.post("/sessions/{session_id}/messages", status_code=status.HTTP_202_ACCEPTED)
async def post_message(
    session_id: uuid.UUID, body: PostMessage, token: Token, chat: Chat, request: Request
) -> MessageAccepted:
    try:
        message_id, masked, cursor = await chat.post_message(session_id, token, body.text)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND) from exc
    except TurnInProgress as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Wait for the current answer to finish"
        ) from exc
    task = asyncio.create_task(chat.run_turn(session_id, message_id, masked, body.version_hint))
    tasks: set[asyncio.Task[None]] = request.app.state.chat_tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return MessageAccepted(message_id=message_id, stream_after=cursor)


@router.get("/sessions/{session_id}/stream")
async def stream(
    session_id: uuid.UUID,
    token: Token,
    chat: Chat,
    after: Annotated[str, Query(pattern=r"^(\$|0|\d+-\d+)$")] = "$",
    last_event_id: Annotated[str | None, Header(pattern=r"^\d+-\d+$")] = None,
) -> StreamingResponse:
    """SSE for one turn: ack, token*, citations, then final | escalated | error, after which
    the stream ends. Read from ``stream_after`` of the POST; ``Last-Event-ID`` (reconnect)
    wins over ``after``."""
    try:
        await chat.check_owner(session_id, token)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND) from exc
    return StreamingResponse(
        chat.events.sse(str(session_id), last_event_id or after),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/sessions/{session_id}")
async def get_transcript(session_id: uuid.UUID, token: Token, chat: Chat) -> Transcript:
    try:
        ticket, messages = await chat.transcript(session_id, token)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND) from exc
    return Transcript(
        session_id=ticket.id,
        status=ticket.status,
        messages=[
            TranscriptMessage(
                id=m.id,
                role=m.role,
                content=m.content_masked,
                citations=m.citations,
                route=m.route,
                created_at=m.created_at,
            )
            for m in messages
        ],
    )


class EscalationAccepted(BaseModel):
    stream_after: str  # read the stream from here to receive the "escalated" event


@router.post("/sessions/{session_id}/escalate", status_code=status.HTTP_202_ACCEPTED)
async def escalate(session_id: uuid.UUID, token: Token, chat: Chat) -> EscalationAccepted:
    try:
        await chat.check_owner(session_id, token)
        cursor = await chat.events.last_id(str(session_id))
        await chat.escalate_on_request(session_id, token)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND) from exc
    return EscalationAccepted(stream_after=cursor)


@router.post("/messages/{message_id}/feedback", status_code=status.HTTP_204_NO_CONTENT)
async def feedback(message_id: uuid.UUID, body: FeedbackIn, token: Token, chat: Chat) -> Response:
    try:
        await chat.add_feedback(message_id, token, body.rating, body.comment)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
