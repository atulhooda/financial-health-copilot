# Hisaab monorepo. `make up && make seed && make demo`.
BACKEND := backend
UV := cd $(BACKEND) && uv run

.PHONY: up down seed demo test train lint

up:            ## Postgres + Redis, then migrations
	docker compose up -d --wait
	$(UV) alembic upgrade head

down:
	docker compose down

train:         ## Train the categoriser and write docs/CATEGORISER.md
	$(UV) hisaab train-categoriser

seed:          ## Personas: A at T0, B and C in full
	$(UV) hisaab seed

demo:          ## Replay persona A T0 -> +3
	$(UV) hisaab demo

test:          ## Full test suite (Postgres-only tests skip if `make up` hasn't run)
	$(UV) pytest

lint:
	$(UV) ruff check app tests
