# Operations

## Workflow A — develop on the Mac, PostgreSQL on the Dell

Prerequisites:
- Python ≥ 3.12;
- the SSH tunnel `ssh -N -L 15432:127.0.0.1:5432 <user>@<dell>`.

Neither Docker nor PostgreSQL is needed on the Mac.

```bash
make venv                 # .venv + pinned dependencies + shared package (editable)
make env                  # .env with generated secrets (mode 0600); then set POSTGRES_PASSWORD
make inventory            # read-only: what is in the database right now?
make migrate-plan         # what would migrate do? (no changes)
make migrate              # only after reviewing the plan
make nodes                # terminal 1: node-1..3 on 127.0.0.1:8101-8103, data in var/nodes/
make register-nodes       # registers NODEMESH_NODES from .env
cd coordinator && ../.venv/bin/python manage.py createsuperuser
make run                  # terminal 2: http://127.0.0.1:8000
make reconcile            # one reconciliation cycle (or: manage.py reconcile --loop)
```

## Workflow B — the cluster in Docker on the Dell

```bash
python3 scripts/init_env.py --cluster     # .env.cluster with generated secrets
# edit .env.cluster: POSTGRES_PASSWORD, POSTGRES_DOCKER_NETWORK (see below)
docker inspect postgres-distributed \
  --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}'
make cluster-up           # build + start against the existing PostgreSQL (migrations NOT automatic)
docker compose --env-file .env.cluster -f compose.yaml -f compose.dell.yaml \
  exec coordinator python manage.py migrate --plan
```

- The coordinator joins the PostgreSQL container's existing network. Nothing
  about `postgres-distributed` (configuration, port, volume) is modified, and
  no second PostgreSQL is started on its port.
- Inside a container, `127.0.0.1` is the container itself. The database is
  reached by container name, never through the Mac's tunnel.
- The coordinator listens on `127.0.0.1:8080` of the Dell by default. From the
  Mac, either forward it (`ssh -L 8080:127.0.0.1:8080 ...`) or set
  `COORDINATOR_BIND` deliberately, ideally behind a TLS reverse proxy.
- Data lives in named volumes (`node-N-data`, `coordinator-spool`), which
  survive `docker compose down` and container recreation. Only `down -v`
  deletes them.

### Isolated test stack (safe anywhere)

```bash
make cluster-test-up      # separate project "nodemesh-test", throwaway PostgreSQL in tmpfs, no host DB port
make demo                 # scripted failure demonstration (see README)
make cluster-test-down    # removes the test stack and its volumes
```

## Adding a storage node

Use the same image or codebase; only the configuration is new.

1. Generate a secret (`python3 -c "import secrets; print(secrets.token_hex(32))"`).
2. Start the instance with `NODE_ID=node-5`, its own volume, and
   `NODE_HMAC_KEYS=k1:<secret>`. In Compose, copy a `node-N` service block,
   which only references the shared `x-node` anchor.
3. Add `node-5:k1:<secret>` to `NODEMESH_NODE_CREDENTIALS` for the coordinator
   and reconciler, then restart them.
4. Run `manage.py register_node node-5 http://node-5:8000`.

New uploads consider the node immediately. Existing files are not rebalanced
(see limitations). The reconciler uses the node for repairs.

## Draining or disabling a node

```bash
manage.py register_node node-2 http://node-2:8000 --state draining   # serves reads, gets no new copies
manage.py register_node node-2 http://node-2:8000 --state disabled   # never contacted
```

Replicas on draining or disabled nodes do not count toward durability, so the
reconciler copies those files elsewhere.

## Rotating a node key

1. Give the node both keys: `NODE_HMAC_KEYS=k2:<new>,k1:<old>`, then restart it.
2. Switch the coordinator to `node-N:k2:<new>`, then restart the coordinator and reconciler.
3. Remove `k1` from the node and restart it.

## Database reset (project tables only)

The rebuilt coordinator uses new tables: `cluster_storagenode`, `files_file`
and `files_replica`. The legacy tables (`node_storagenode`, `node_file`,
`node_filechunk`) do not conflict with them and can be left in place.

To start from a clean schema:

1. Run `make inventory` (read-only) and review exactly which tables exist and
   how many rows they hold.
2. Take a backup on the Dell:
   `docker exec postgres-distributed pg_dump -U storage_admin -Fc distributed_storage > backup.dump`.
3. Execute only the statements that were presented and approved for this
   database. The reset never touches other databases on the server.

## Logs and monitoring

All services log one JSON object per line to stdout:
- `docker compose logs -f coordinator reconciler node-1`
- useful fields: `file_id`, `node_id`, `error`, `report`.

Each reconciler cycle logs its counters. For example, `replicas_created`,
`replicas_corrupt` or `orphans_deleted` are worth alerting on.

Probes:
- the coordinator's `/readyz` checks the database;
- a node's `/readyz` checks that its volume is writable and above the free-space floor.

## Troubleshooting

| Symptom | Check |
|---|---|
| Uploads return 503 `replication_quorum_not_met` | `GET /api/v1/cluster/nodes/` (staff): fewer than W nodes are active and not suspect; are node keys consistent between the coordinator and the nodes (`bad_signature` in `last_error`)? |
| A node stays `suspect` after it was fixed | The next reconciler cycle probes it; run `manage.py reconcile` once |
| `expired` signatures | The clocks of the coordinator and node hosts differ by more than 60 s: enable NTP |
| Files stay under-replicated | Is there an eligible node without a copy (`re_replication_no_target` in the reconciler log)? With 3 nodes and one down, there is none until it returns |
