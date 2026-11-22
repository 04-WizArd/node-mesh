# Development (Workflow A)

Run the coordinator and three storage nodes directly from a Python virtualenv,
against a PostgreSQL database of your choice. Docker is not needed. This is
the fastest edit–run–test loop.

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Python | 3.12+ (CI uses 3.13) | `python3 --version` |
| make, git | any recent | preinstalled on macOS (Xcode Command Line Tools) and most Linux distributions |
| PostgreSQL | 14+ (17 is used in CI) | local, or a remote server reached through an SSH tunnel |

The test suite needs **no** database at all (see [testing.md](testing.md)).

## 1. Install

```bash
git clone https://github.com/<owner>/node-mesh.git && cd node-mesh
make venv          # .venv with pinned dependencies + the shared package
make test          # optional: everything should pass before you start
```

## 2. Create a development database

Use a database that belongs to development only: **never** the database of
a running Docker cluster. Two coordinators on one database interfere: each
registers its own node URLs, probes nodes it cannot reach, and
garbage-collects according to the other's metadata. `register_node` refuses
to repoint registered nodes, and orphan cleanup refuses mass deletions, but
separate databases avoid the problem entirely.

In `psql` as an administrative role:

```sql
CREATE ROLE nodemesh LOGIN;
\password nodemesh
CREATE DATABASE nodemesh_dev OWNER nodemesh;
```

`\password` asks for the password interactively, so it never appears in the
shell history.

If the server is remote, open a tunnel in its own terminal and keep it open:

```bash
ssh -N -L 15432:127.0.0.1:5432 <user>@<db-server>
```

If PostgreSQL itself runs in Docker on that server, the container must
publish its port on the server's loopback interface (for example
`127.0.0.1:5432:5432`).

## 3. Configure

```bash
make dev-env       # writes .env (mode 0600) with a generated Django key and node keys
```

Edit `.env` and set `POSTGRES_PASSWORD`. Adjust `POSTGRES_HOST`, `POSTGRES_PORT`,
`POSTGRES_DB` and `POSTGRES_USER` if yours differ. `.env` is git-ignored.
Real environment variables always override it.

## 4. Initialise the schema

```bash
make dev-inventory      # read-only: what does this database contain right now?
make dev-migrate-plan   # which migrations would run (no changes)
make dev-migrate
make dev-superuser      # an admin account (staff users also see the cluster panel in the UI)
```

## 5. Run

```bash
make dev-nodes          # terminal 1: node-1..3 on 127.0.0.1:8101-8103, data in var/nodes/
make dev-register       # once: registers NODEMESH_NODES from .env
make dev-run            # terminal 2: http://127.0.0.1:8000
```

The UI is in French; the API is documented in [api.md](api.md).

There is no background reconciler in this workflow. Run cycles by hand:

```bash
make dev-reconcile      # one cycle: probes, repairs, garbage collection
make dev-status         # node health and replication summary
```

Or keep it running in another terminal:
`cd coordinator && ../.venv/bin/python manage.py reconcile --loop`.

## 6. Try a failure

1. Upload a file in the UI.
2. Stop the `dev-nodes` terminal (all three nodes stop), restart it, and
   download the file again.
3. To stop only one node, run the nodes separately (see `scripts/run_nodes.py`)
   or use the Docker workflow, where `scripts/cluster_demo.py` automates the
   whole scenario.

## Everyday commands

| Command | Purpose |
|---|---|
| `make test` / `make test-fast` | all suites / coordinator without real node processes |
| `make lint` / `make format` | ruff check / apply fixes and formatting |
| `make check` | Django system checks and migration drift |
| `cd coordinator && ../.venv/bin/python manage.py makemigrations` | after model changes; commit the migration |

## Resetting local data

Local node data lives in `var/nodes/` (git-ignored). The database is the one
you configured. To start over, drop and recreate **your development database
only**, then delete `var/nodes/`. Do both together: a database without the
objects, or objects without the database, is exactly the inconsistency the
reconciler is designed to repair.
