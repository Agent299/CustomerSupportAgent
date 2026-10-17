from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import Settings, get_settings
from app.ingest_api.routes import router as ingest_router
from shared.db.session import make_engine, make_session_factory
from shared.logging import configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, json=settings.app_env != "local")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings.database_url)  # connects lazily, on first use
        app.state.sessions = make_session_factory(engine)
        yield
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
        """Liveness: the process is up. Render's health check and the widget's wake-up ping."""
        return {"status": "ok"}

    @app.get("/readyz", tags=["health"])
    async def readyz() -> dict[str, str]:
        """Readiness. Dependency checks are added as each datastore is wired in (Phase 1+)."""
        return {"status": "ready", "env": settings.app_env}

    app.include_router(ingest_router)
    return app


app = create_app()
