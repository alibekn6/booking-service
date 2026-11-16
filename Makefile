.PHONY: up down logs infra test lint typecheck fmt check loadtest audit token

up:            ## build and start the whole stack (api on :8080)
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f api sweeper

infra:         ## only Postgres + Redis, for tests and local runs
	docker compose up -d --wait postgres redis

test: infra
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy

fmt:
	uv run ruff format .
	uv run ruff check --fix .

check: lint typecheck test

loadtest:      ## 10k buyers / 1000 seats, then audit the DB (runs even if thresholds fail)
	-docker compose --profile loadtest run --rm k6
	$(MAKE) audit

audit:         ## no double sale, no stuck seat, limits respected
	docker compose exec -T api python -m app.cli check-invariants

token:         ## make token ROLE=organizer|buyer
	@docker compose exec -T api python -m app.cli issue-token --role $(or $(ROLE),buyer)
