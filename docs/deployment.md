# Docker deployment (Workflow B)

Run the whole cluster with Docker Compose on one Linux host: the coordinator,
the reconciler and three storage nodes. The cluster uses an existing PostgreSQL
container on that host.

```
host
├── coordinator   :8080 on 127.0.0.1 (the only published port)
├── reconciler    background repairs, no port
├── node-1..3     same image, one volume each, no port, internal network only
└── PostgreSQL    your existing container, joined through its Docker network
```

The responsibilities of each service, replication and failure handling are
explained in [architecture.md](architecture.md); the guarantees are in
[consistency.md](consistency.md).

## Prerequisites

| Requirement | Check |
|---|---|
| Linux with Docker Engine 24+ and the Compose plugin 2.20+ | `docker version`, `docker compose version` |
| GNU make, git, python3 (standard library only, used to generate secrets) | `make --version`, `python3 --version` |
| A PostgreSQL 14+ container on the same host | `docker ps` |
| About 1 GB free disk for images, plus room for the data volumes | `df -h /var/lib/docker` |

All commands below run on the Docker host, from the repository directory.

## 1. Get the code

```bash
git clone https://github.com/<owner>/node-mesh.git && cd node-mesh
```

Clone into a directory you will keep. Compose derives the **project name**, and
therefore the volume names, from the directory name (see [Volumes](#volumes)).

## 2. Create a database for the cluster

Give the cluster its own role and database, inside the existing PostgreSQL
container:

```bash
docker exec -it <postgres-container> psql -U <admin-role> -d postgres
```
```sql
CREATE ROLE nodemesh LOGIN;
\password nodemesh
CREATE DATABASE nodemesh OWNER nodemesh;
```

### Separate databases

The cluster's database must not be shared with a development coordinator
([development.md](development.md)). Two coordinators on one database interfere:
- each tries to register its own node URLs;
- a development reconciler probes nodes it cannot reach and garbage-collects
  against the cluster's metadata.

Two guards limit the damage, but they are not a reason to share a database:
- `register_node` refuses to change a registered node's URL without `--update-url`;
- orphan cleanup refuses to delete when an implausibly large share of a node's
  objects is unknown.

A dedicated role that owns only its own database also limits what the
application could do if it were compromised. Do not run the cluster with a
PostgreSQL superuser.

## 3. Configure

```bash
make cluster-env     # writes .env.cluster (mode 0600) with generated secrets
```

Then fill in the three `<fill-in>` values:

| Variable | Value |
|---|---|
| `POSTGRES_CONTAINER_HOST` | the PostgreSQL container's name, e.g. `postgres` |
| `POSTGRES_DOCKER_NETWORK` | a network that container is attached to (command below) |
| `POSTGRES_PASSWORD` | the password set with `\password` |

```bash
docker inspect <postgres-container> \
  --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}'
```

**How the database is reached.** The coordinator and the reconciler join that
existing network and connect to the container by name on port 5432. Nothing
about the PostgreSQL container changes (configuration, published ports,
volumes), and no second PostgreSQL is started. Inside a container,
`127.0.0.1` means the container itself, so a host port or an SSH tunnel would
not work here.

Other settings in `.env.cluster` (replication factor, write quorum, upload
limit, reconcile interval) are documented inline. Keep `.env.cluster` out of
Git (it is ignored) and back it up securely: it holds the node keys.

Check the result:

```bash
make cluster-config  # validates the fully resolved Compose configuration
```

## 4. Build, migrate, start

```bash
make cluster-build          # builds the two images: coordinator, storage-node
make cluster-migrate-plan   # lists the migrations that would run (no changes)
make cluster-migrate        # applies them
make cluster-up             # starts everything and waits until all services are healthy
make cluster-superuser      # an admin account for the UI
```

- **Images.** One image serves all storage nodes, and one serves the
  coordinator and the reconciler. Only `coordinator` and `node-1` build. The
  other services reuse the local image (`pull_policy: never`), so images are
  built once, never concurrently, and never looked up in a registry.
  `cluster-up` never builds: run `cluster-build` first.
- **Migrations are always explicit.** Starting the cluster never migrates the
  database. If the schema is behind the code, or the database is unreachable,
  the coordinator stops at startup with a message naming the fix. The reconciler
  stays unhealthy until it can complete a cycle.
- **What "healthy" means.** `cluster-up` waits for each service's healthcheck:
  - storage nodes: the volume is writable and above the free-space floor;
  - coordinator: the database is reachable (`/readyz`);
  - reconciler: a cycle succeeded within the last `NODEMESH_RECONCILER_MAX_SILENCE`
    seconds (a heartbeat file written after each successful cycle).

## 5. Use it from another machine

The coordinator is published on the host's loopback interface only. From your
workstation, open an SSH tunnel and browse to `http://127.0.0.1:8080`:

```bash
ssh -N -L 8080:127.0.0.1:8080 <user>@<docker-host>
```

To expose the coordinator on the network instead, put a TLS reverse proxy in
front of it. Then set `DJANGO_HTTPS=true`, `DJANGO_TRUST_X_FORWARDED_PROTO=true`,
`DJANGO_ALLOWED_HOSTS=<public name>` and `DJANGO_CSRF_TRUSTED_ORIGINS=https://<public name>`
(see [security.md](security.md)).

## 6. Operate

| Command | Purpose |
|---|---|
| `make cluster-ps` | container state and health |
| `make cluster-status` | node health, free space, copies per node, under-replicated files |
| `make cluster-storage` | read-only: per storage node, its state, its `/data` volume, and each stored object's id, size and recorded SHA-256 |
| `make cluster-logs SERVICE=reconciler` | follow one service's JSON logs (omit `SERVICE` for all) |
| `make cluster-reconcile` | run one reconciliation cycle now |
| `make cluster-restart SERVICE=node-2` | restart one service |
| `make cluster-stop` / `make cluster-up` | stop everything (containers kept) / start again |
| `make cluster-down` | remove containers and networks; **volumes are kept** |
| `make cluster-volumes` | list the data volumes |

Failure handling, adding nodes, key rotation and backups are covered in
[operations.md](operations.md).

## Updating after new commits

```bash
git pull --ff-only
make cluster-config          # the configuration still resolves (new variables?)
make cluster-build
make cluster-migrate-plan    # does this version change the schema?
make cluster-migrate         # only if the plan lists migrations; take a backup first
make cluster-up              # recreates only the containers whose image or config changed
make cluster-ps && make cluster-status
```

- **Brief interruption.** Recreated containers restart together, so expect a
  few seconds without service. Rolling restarts are not implemented.
- **Rolling back.** `git checkout <previous commit>`, then `make cluster-build`
  and `make cluster-up`. If the newer version applied migrations, restore the
  database backup taken before migrating, or check that its migrations are
  backward compatible.

## Volumes

| Volume | Content | If lost |
|---|---|---|
| `<project>_node-N-data` | that node's objects | recoverable while other copies exist: `make cluster-rescan NODE=node-N`, then repair (see operations) |
| `<project>_coordinator-spool` | temporary upload/download files only | nothing of value |

- Data lives in named volumes, which survive `cluster-stop`, `cluster-down`,
  image rebuilds and container recreation.
- `<project>` is the Compose project name (by default, the repository
  directory's name). If you move the checkout or set `COMPOSE_PROJECT_NAME`,
  Compose looks for **different** volumes and the cluster starts empty. The old
  volumes still exist, and pointing back at the old project name finds them again.
- **Commands that destroy data:** `docker compose down -v`, `docker volume rm`,
  `docker volume prune`, `docker system prune --volumes`. None of the Makefile
  targets for the cluster uses them. `make cluster-test-down` removes volumes
  of the isolated `nodemesh-test` project only.
- **One disk, one failure domain.** All three node volumes are on the same
  disk of the same host. Replication protects against a node process or a
  volume failing, not against losing the host or its disk. For real
  independence, run nodes on separate machines, and take backups
  ([operations.md](operations.md#backups)).

## Isolated test stack

A second, disposable stack can run next to the real one. It uses:
- its own Compose project (`nodemesh-test`);
- a PostgreSQL in tmpfs with no published port;
- images tagged `test`;
- port 18080.

```bash
make cluster-test-up        # build + start + wait for health
make cluster-test-demo      # upload, stop node-2, verified read, quorum write, restart, repair
make cluster-test-down      # removes the test stack and its volumes only
```

CI runs exactly these commands.
