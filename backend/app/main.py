import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from decimal import Decimal

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from opensearchpy import AsyncOpenSearch
from valkey.asyncio import Valkey

from app.chat.events import ChatEvents
from app.chat.routes import router as chat_router
from app.chat.service import ChatService
from app.config import Settings, get_settings
from app.genai.embeddings import make_embedder
from app.genai.llm import make_llm
from app.ingest_api.routes import router as ingest_router
from app.orchestrator.pipeline import QueryDeps
from app.retrieval import kb_index
from shared.db.session import make_engine, make_session_factory
from shared.logging import configure_logging

log = structlog.get_logger()

QueryDepsFactory = Callable[[], QueryDeps]


def create_app(
    settings: Settings | None = None, query_deps: QueryDepsFactory | None = None
) -> FastAPI:
    """``query_deps`` replaces the real search + OpenAI stack (tests)."""
    settings = settings or get_settings()
    configure_logging(settings.log_level, json=settings.app_env != "local")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings.database_url)  # connects lazily, on first use
        sessions = make_session_factory(engine)
        app.state.sessions = sessions
        kv = Valkey.from_url(settings.redis_url, decode_responses=True)
        search = kb_index.make_client(settings)
        app.state.chat_tasks = set()
        app.state.chat = None
        factory = query_deps or _real_query_deps(settings, search)
        if factory is not None:
            app.state.chat = ChatService(
                sessions=sessions,
                kv=kv,
                events=ChatEvents(kv),
                query_deps=factory,
                spend_cap_usd=Decimal(str(settings.daily_spend_cap_usd)),
            )
        yield
        for task in list(app.state.chat_tasks):
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*app.state.chat_tasks, return_exceptions=True)
        await search.close()
        await kv.aclose()
        await engine.dispose()

    app = FastAPI(title="Support Agent API", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        """Liveness: the process is up (container healthcheck, UI status badge)."""
        return {"status": "ok"}

    @app.get("/readyz", tags=["health"])
    async def readyz() -> dict[str, str]:
        """Readiness. Dependency checks are added as each datastore is wired in (Phase 1+)."""
        return {"status": "ready", "env": settings.app_env}

    app.include_router(ingest_router)
    app.include_router(chat_router)
    return app


def _real_query_deps(settings: Settings, search: AsyncOpenSearch) -> QueryDepsFactory | None:
    """Search + OpenAI, or ``None`` (chat answers 503) while the models aren't configured."""
    if settings.openai_api_key is None or not settings.model_hard:
        log.warning("chat.disabled", reason="OPENAI_API_KEY or MODEL_HARD not set")
        return None
    index = kb_index.KbIndex(search, settings.opensearch_index, settings.embedding_dimensions)
    embedder = make_embedder(settings)
    llm = make_llm(settings)
    return lambda: QueryDeps(index, embedder, llm.metered())


app = create_app()
