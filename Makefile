# Hisaab monorepo. `make up && make seed && make demo`.
BACKEND := backend
UV := cd $(BACKEND) && uv run

.PHONY: up down seed demo test train lint eval secret-scan hooks

up:            ## Postgres + Redis, then migrations
	docker compose up -d --wait
	$(UV) alembic upgrade head

down:
	docker compose down

train:         ## Train the categoriser and write docs/CATEGORISER.md
	$(UV) hisaab train-categoriser

seed:          ## Migrations, then personas: A at T0, B and C in full
	$(UV) alembic upgrade head
	$(UV) hisaab seed

demo:          ## Replay persona A T0 -> +3
	$(UV) hisaab demo

test:          ## Full test suite (Postgres-only tests skip if `make up` hasn't run)
	$(UV) pytest

lint:
	$(UV) ruff check app tests

eval:          ## Copilot eval on every candidate with a key in backend/.env, then the template baseline
	$(UV) hisaab eval-copilot --candidates
	$(UV) hisaab eval-copilot --provider none

secret-scan:   ## Fail if anything credential-like is tracked or about to be committed (runs before every push)
	python3 tools/secret_scan.py

hooks:         ## Install the pre-push secret scan
	cp tools/pre-push .git/hooks/pre-push && chmod +x .git/hooks/pre-push

