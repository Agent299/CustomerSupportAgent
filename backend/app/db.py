from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One transaction per request; committed if the handler returns normally."""
    sessions: async_sessionmaker[AsyncSession] = request.app.state.sessions
    async with sessions.begin() as session:
        yield session
