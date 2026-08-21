# GIMME Retention Engine — common tasks.
#
#   make setup   install dependencies (once)
#   make seed    create the database and generate demo data
#   make backend / make frontend    run a dev server
#   make test    run every test suite

.PHONY: help setup setup-backend setup-frontend init seed reset backend frontend \
        test test-backend test-frontend test-e2e lint build docker-up docker-down clean

VENV := backend/.venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: setup-backend setup-frontend ## Install backend and frontend dependencies

setup-backend: ## Create the Python venv and install backend dependencies
	python3 -m venv $(VENV)
	$(PIP) install --quiet --upgrade pip
	$(PIP) install --quiet -r backend/requirements.txt
	@echo "Backend dependencies installed."

setup-frontend: ## Install frontend dependencies
	cd frontend && npm install --no-audit --no-fund
	@echo "Frontend dependencies installed."

init: ## Create the database schema and baseline configuration
	cd backend && ../$(PY) -m scripts.init_db

seed: ## Generate 1,000 synthetic customers with 12 months of history
	cd backend && ../$(PY) -m scripts.seed_demo --customers 1000 --reset

seed-small: ## Generate a 120-customer dataset (faster)
	cd backend && ../$(PY) -m scripts.seed_demo --customers 120 --reset

reset: ## Delete the local database, then re-seed from scratch
	rm -rf data/gimme.db data/gimme.db-wal data/gimme.db-shm
	$(MAKE) seed

backend: ## Run the API on http://127.0.0.1:8000
	cd backend && ../$(PY) -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

frontend: ## Run the UI on http://127.0.0.1:5173
	cd frontend && npm run dev

test: test-backend test-frontend ## Run backend and frontend unit/integration tests

test-backend: ## Run the backend test suite
	cd backend && ../$(PY) -m pytest

test-frontend: ## Run the frontend unit tests
	cd frontend && npm test

test-e2e: ## Run the browser tests (needs backend and frontend already running)
	cd frontend && npm run test:e2e

lint: ## Lint the frontend
	cd frontend && npm run lint

build: ## Type-check and build the frontend for production
	cd frontend && npm run build

docker-up: ## Start the whole stack with Docker Compose
	docker compose up --build -d
	@echo "Frontend http://127.0.0.1:5173   API http://127.0.0.1:8000/docs"

docker-down: ## Stop the Docker Compose stack
	docker compose down

clean: ## Remove caches, build output and the local database
	rm -rf data frontend/dist frontend/node_modules/.vite \
	       backend/.pytest_cache frontend/test-results frontend/playwright-report
	find backend -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	@echo "Cleaned. Run 'make seed' to rebuild the database."
