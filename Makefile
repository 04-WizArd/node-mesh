# NodeMesh developer and operator commands.  `make help` lists them.
#
#   dev-*          Workflow A: Django + storage nodes run locally (Python venv),
#                  PostgreSQL reached through POSTGRES_* in .env.
#   cluster-*      Workflow B: the Docker cluster (compose.yaml + compose.external-db.yaml),
#                  configured by .env.cluster, using an existing PostgreSQL container.
#   cluster-test-* An isolated, throwaway Docker stack (own project, own PostgreSQL in
#                  tmpfs, own image tag and port). Safe to run next to the real cluster.
#
# No target ever deletes the persistent cluster's volumes, and no target
# migrates a shared database implicitly (use the *-migrate targets).

PY        ?= .venv/bin/python
ENV_FILE  ?= .env.cluster
SERVICE   ?=
NODE      ?=
TEST_PORT ?= 18080

COMPOSE      = docker compose --env-file $(ENV_FILE) -f compose.yaml -f compose.external-db.yaml
# Explicit project name: `down -v` below can only ever reach the test stack.
TEST_COMPOSE = env NODEMESH_VERSION=test COORDINATOR_PORT=$(TEST_PORT) \
               docker compose --env-file $(ENV_FILE) -p nodemesh-test -f compose.yaml -f compose.test.yaml
MANAGE       = cd coordinator && ../$(PY) manage.py
# One-off coordinator commands that must bypass the entrypoint's schema check.
CLUSTER_MANAGE = $(COMPOSE) run --rm --no-deps --entrypoint python coordinator manage.py

.DEFAULT_GOAL := help
.PHONY: help venv lint format test test-shared test-node test-coordinator test-fast check \
        dev-env dev-inventory dev-migrate-plan dev-migrate dev-superuser dev-nodes dev-register \
        dev-run dev-reconcile dev-status \
        cluster-env cluster-config cluster-build cluster-migrate-plan cluster-migrate cluster-up \
        cluster-ps cluster-logs cluster-status cluster-storage cluster-reconcile cluster-rescan cluster-superuser cluster-restart \
        cluster-stop cluster-down cluster-volumes cluster-spare-up \
        cluster-test-up cluster-test-demo cluster-test-logs cluster-test-down \
        _require-dev-env _require-cluster-env

help: ## List commands
	@awk 'BEGIN {FS = ":.*## "} /^##@/ {printf "\n%s\n", substr($$0, 5)} \
	     /^[a-z][a-z-]+:.*## / {printf "  %-22s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

##@ Setup and quality (no database or Docker needed)
venv: ## Create .venv with pinned dependencies and the shared package
	python3 -m venv .venv && $(PY) -m pip install -r requirements/dev.txt

lint: ## Ruff lint and format check
	$(PY) -m ruff check . && $(PY) -m ruff format --check .

format: ## Apply ruff fixes and formatting
	$(PY) -m ruff check --fix . && $(PY) -m ruff format .

test-shared:
	cd shared && ../$(PY) -m pytest -q
test-node:
	cd storage-node && ../$(PY) -m pytest -q
test-coordinator:
	cd coordinator && ../$(PY) -m pytest -q

test: test-shared test-node test-coordinator ## All test suites (never uses the configured database)

test-fast: ## Coordinator tests without real storage-node processes
	cd coordinator && ../$(PY) -m pytest -q -m "not cluster"

check: ## Django system checks and migration drift
	$(MANAGE) check --settings=config.test_settings
	$(MANAGE) makemigrations --check --dry-run --settings=config.test_settings

##@ Workflow A: local development (.env)
dev-env: ## Create .env with generated secrets (then set POSTGRES_PASSWORD)
	$(PY) scripts/init_env.py

dev-inventory: _require-dev-env ## Read-only inventory of the configured database
	$(PY) scripts/db_inventory.py

dev-migrate-plan: _require-dev-env ## Show pending migrations (no changes)
	$(MANAGE) migrate --plan

dev-migrate: _require-dev-env ## Apply migrations to the .env database
	$(MANAGE) migrate

dev-superuser: _require-dev-env ## Create an admin account
	$(MANAGE) createsuperuser

dev-nodes: _require-dev-env ## Run node-1..3 on 127.0.0.1:8101-8103 (Ctrl-C to stop)
	$(PY) scripts/run_nodes.py

dev-register: _require-dev-env ## Register the nodes listed in NODEMESH_NODES
	$(MANAGE) register_node --from-env

dev-run: _require-dev-env ## Coordinator dev server on http://127.0.0.1:8000
	$(MANAGE) runserver 127.0.0.1:8000

dev-reconcile: _require-dev-env ## One reconciliation cycle
	$(MANAGE) reconcile

dev-status: _require-dev-env ## Node health and replication summary
	$(MANAGE) cluster_status

##@ Workflow B: Docker cluster (.env.cluster)
cluster-env: ## Create .env.cluster with generated secrets
	python3 scripts/init_env.py --cluster

cluster-config: _require-cluster-env ## Validate the resolved Compose configuration
	$(COMPOSE) config --quiet
	@echo "Compose configuration is valid. Services:"
	@$(COMPOSE) config --services | sed 's/^/  /'

cluster-build: _require-cluster-env ## Build the two images (coordinator, storage-node)
	$(COMPOSE) build

cluster-migrate-plan: _require-cluster-env ## Show pending migrations (no changes)
	$(CLUSTER_MANAGE) migrate --plan

cluster-migrate: _require-cluster-env ## Apply migrations (run deliberately, after the plan)
	$(CLUSTER_MANAGE) migrate --noinput

cluster-up: _require-cluster-env ## Start or update the cluster and wait until healthy
	$(COMPOSE) up -d --no-build --wait --wait-timeout 180

cluster-ps: _require-cluster-env ## Container status and health
	$(COMPOSE) ps

cluster-logs: _require-cluster-env ## Follow logs (SERVICE=coordinator|reconciler|node-1...)
	$(COMPOSE) logs -f --tail=200 $(SERVICE)

cluster-status: _require-cluster-env ## Node health and replication summary
	$(COMPOSE) exec coordinator python manage.py cluster_status

cluster-storage: _require-cluster-env ## Read-only: per node, volume, objects and their SHA-256
	@COMPOSE="$(COMPOSE)" sh scripts/cluster_storage.sh

cluster-reconcile: _require-cluster-env ## Run one reconciliation cycle now
	$(COMPOSE) exec reconciler python manage.py reconcile

cluster-rescan: _require-cluster-env ## Re-verify every copy on NODE=node-N (after a volume loss)
	@test -n "$(NODE)" || { echo "usage: make cluster-rescan NODE=node-2"; exit 1; }
	$(COMPOSE) exec coordinator python manage.py rescan_node $(NODE)

cluster-superuser: _require-cluster-env ## Create an admin account
	$(COMPOSE) exec coordinator python manage.py createsuperuser

cluster-restart: _require-cluster-env ## Restart containers (SERVICE=... for one)
	$(COMPOSE) restart $(SERVICE)

cluster-stop: _require-cluster-env ## Stop containers, keep them and all volumes
	$(COMPOSE) stop

cluster-down: _require-cluster-env ## Remove containers and networks, KEEP volumes
	$(COMPOSE) down

cluster-volumes: ## List NodeMesh volumes (data lives here)
	@docker volume ls --format '{{.Name}}' | grep -E '_(node-[0-9]+-data|coordinator-spool)$$' || echo "no NodeMesh volume found"

cluster-spare-up: _require-cluster-env ## Start the spare node-4 (then register it)
	$(COMPOSE) --profile spare up -d --no-build --wait node-4

##@ Isolated test stack (throwaway PostgreSQL, project "nodemesh-test", port 18080 by default)
cluster-test-up: _require-cluster-env ## Build and start the isolated test stack
	$(TEST_COMPOSE) build
	$(TEST_COMPOSE) up -d --no-build --wait --wait-timeout 180

cluster-test-demo: _require-cluster-env ## Scripted failure demonstration against the test stack
	python3 scripts/cluster_demo.py --create-user --url http://127.0.0.1:$(TEST_PORT) --compose "$(TEST_COMPOSE)"

cluster-test-logs: _require-cluster-env ## Recent logs of the test stack
	$(TEST_COMPOSE) logs --no-color --tail=200

cluster-test-down: _require-cluster-env ## Remove the test stack and ITS volumes only
	$(TEST_COMPOSE) down -v --remove-orphans

_require-dev-env:
	@test -f .env || { echo "Missing .env: run 'make dev-env' and set POSTGRES_PASSWORD (docs/development.md)."; exit 1; }

_require-cluster-env:
	@test -f $(ENV_FILE) || { echo "Missing $(ENV_FILE): run 'make cluster-env' (docs/deployment.md)."; exit 1; }
