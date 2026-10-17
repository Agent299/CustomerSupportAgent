COMPOSE := docker compose -f infra/docker-compose.yml

.PHONY: help up up-app down nuke ps logs api web install test lint typecheck check

help: ## List targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

up: ## Start datastores and wait until healthy
	$(COMPOSE) up -d --wait

up-app: ## Start datastores + containerized API
	$(COMPOSE) --profile app up -d --build --wait

down: ## Stop containers (keep data)
	$(COMPOSE) --profile app down

nuke: ## Stop containers and delete all local data volumes
	$(COMPOSE) --profile app down -v

ps: ## Show container health
	$(COMPOSE) ps

logs: ## Tail logs
	$(COMPOSE) logs -f --tail=100

install: ## Install backend + web dependencies
	cd backend && uv sync
	cd web && pnpm install

api: ## Run the API on the host with reload
	cd backend && uv run uvicorn app.main:app --reload --port 8000

web: ## Run the Next.js dev server
	cd web && pnpm dev

test: ## Backend tests
	cd backend && uv run pytest

lint: ## Lint backend + web
	cd backend && uv run ruff check . && uv run ruff format --check .
	cd web && pnpm lint

typecheck: ## mypy (strict)
	cd backend && uv run mypy

check: lint typecheck test ## Everything CI runs
	cd web && pnpm build
