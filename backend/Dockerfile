# Backend images.
#   target "worker": ingestion workers (app + shared + workers)
#   target "api" (default, last stage): the FastAPI service (app + shared only)
FROM python:3.13-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /srv

# Dependencies first for layer caching.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY shared ./shared
RUN useradd --system --uid 10001 --create-home appuser


FROM base AS worker
COPY alembic.ini ./
COPY migrations ./migrations
COPY workers ./workers
# tiktoken caches its tokenizer file here (mounted as a volume in production).
ENV TIKTOKEN_CACHE_DIR=/srv/.cache/tiktoken
RUN mkdir -p /srv/.cache/tiktoken && chown -R appuser /srv/.cache
USER appuser
ENTRYPOINT ["python", "-m", "workers.ingest.run"]


FROM base AS api
USER appuser
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers", "--forwarded-allow-ips", "*"]
