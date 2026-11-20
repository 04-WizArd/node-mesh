# NodeMesh developer commands. Run `make help`.
PY      ?= .venv/bin/python
COMPOSE ?= docker compose --env-file .env.cluster -f compose.yaml
MANAGE  := cd coordinator && ../$(PY) manage.py

.PHONY: help venv env lint format test test-shared test-node test-coordinator test-fast check \
        nodes migrate-plan migrate register-nodes run reconcile inventory \
        cluster-up cluster-down cluster-test-up cluster-test-down demo

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-20s %s\n", $$1, $$2}'

venv: ## Create .venv and install everything for development
	python3 -m venv .venv && $(PY) -m pip install -r requirements/dev.txt

env: ## Create .env with generated secrets (Workflow A)
	$(PY) scripts/init_env.py

lint: ## Ruff lint + format check
	$(PY) -m ruff check . && $(PY) -m ruff format --check .

format: ## Apply ruff formatting and safe fixes
	$(PY) -m ruff check --fix . && $(PY) -m ruff format .

test-shared:
	cd shared && ../$(PY) -m pytest -q
test-node:
	cd storage-node && ../$(PY) -m pytest -q
test-coordinator:
	cd coordinator && ../$(PY) -m pytest -q
test: test-shared test-node test-coordinator ## All test suites (never touches the dev database)

test-fast: ## Coordinator tests without real node processes
	cd coordinator && ../$(PY) -m pytest -q -m "not cluster"

check: ## Django system checks and migration drift check (test settings)
	$(MANAGE) check --settings=config.test_settings
	$(MANAGE) makemigrations --check --dry-run --settings=config.test_settings

# --- Workflow A: Django on the Mac, PostgreSQL on the Dell via the SSH tunnel ---------
nodes: ## Run node-1..3 locally (ports 8101-8103)
	$(PY) scripts/run_nodes.py

inventory: ## Read-only inventory of the configured database
	$(PY) scripts/db_inventory.py

migrate-plan: ## Show which migrations would run (no changes)
	$(MANAGE) migrate --plan

migrate: ## Apply migrations to the configured database
	$(MANAGE) migrate

register-nodes: ## Register the nodes listed in NODEMESH_NODES
	$(MANAGE) register_node --from-env

run: ## Coordinator dev server on http://127.0.0.1:8000
	$(MANAGE) runserver 127.0.0.1:8000

reconcile: ## One reconciliation cycle
	$(MANAGE) reconcile

# --- Workflow B: Docker on the Dell ----------------------------------------------------
cluster-up: ## Start the cluster against the existing Dell PostgreSQL
	$(COMPOSE) -f compose.dell.yaml up -d --build --wait

cluster-down: ## Stop the cluster (volumes are kept)
	$(COMPOSE) -f compose.dell.yaml down

cluster-test-up: ## Start the isolated test cluster (throwaway PostgreSQL)
	$(COMPOSE) -f compose.test.yaml up -d --build --wait

cluster-test-down: ## Remove the isolated test cluster INCLUDING its volumes
	$(COMPOSE) -f compose.test.yaml down -v

demo: ## Failure demonstration against the isolated test cluster
	python3 scripts/cluster_demo.py --create-user --compose "$(COMPOSE) -f compose.test.yaml"
