# Operations

Day-2 runbook for a running cluster. Installation:
- [development.md](development.md) (Workflow A, local processes);
- [deployment.md](deployment.md) (Workflow B, Docker).

Commands are given for the Docker cluster. In Workflow A, use the matching
`dev-*` target, or `manage.py` directly.

## Reading the cluster's state

```bash
make cluster-ps       # containers: running / healthy
make cluster-status   # nodes and replication, from the coordinator's metadata
```

`cluster-status` shows, per node:
- `admin` (`active` / `draining` / `disabled`): the operator's intent;
- `health`, as observed by the coordinator:

  | `health` | Meaning |
  |---|---|
  | `healthy` | the last contact succeeded |
  | `unreachable` | a few recent failures; possibly transient, the node still receives copies as a last resort |
  | `suspect` | `NODEMESH_SUSPECT_AFTER_FAILURES` consecutive failures (default 3): presumed down, its copies stop counting and repairs start |
  | `unknown` | never contacted |

- `copies`: the stored replicas it holds.

The summary line counts files per status and the **under-replicated** ones,
meaning files with fewer healthy copies than the replication factor.

The reconciler runs every `NODEMESH_RECONCILE_INTERVAL` seconds (default 30).
Each cycle logs one JSON line with counters, which you can read with
`make cluster-logs SERVICE=reconciler`:

| Counter | Meaning | Worth attention when |
|---|---|---|
| `nodes_healthy` / `nodes_failing` | probe results | `nodes_failing` > 0 for several cycles |
| `replicas_created` | repairs (re-replication) | persistently high |
| `replicas_corrupt`, `replicas_missing` | damaged or lost copies found | any |
| `orphans_deleted` | garbage removed from nodes | unexpectedly high |
| `orphan_gc_refused` | mass deletion blocked (likely wrong database) | **any: investigate before anything else** |
| `re_replication_no_target` | no eligible node to receive a copy | persistent: add a node or bring one back |

## Failure scenarios

### A storage node stops or crashes

**Impact.**
- Reads continue from the other copies.
- Uploads still succeed with two nodes (W = 2) and are reported as under-replicated.

**Recovery.** Docker restarts the node (`restart: unless-stopped`). Otherwise:
`make cluster-restart SERVICE=node-2`.

Once the node is healthy again, the next reconciler cycles copy the missing
replicas back to it.

With three nodes and RF = 3, there is no spare to copy to while a node is down:
files stay under-replicated until it returns. A spare node changes this (see
below).

### A node's volume is lost or replaced

A node that restarts with an empty or restored volume looks healthy, because
it answers probes. Its missing copies are only noticed when they are read or
re-verified (`NODEMESH_VERIFY_INTERVAL_SECONDS`, 24 h by default). To force
the check:

```bash
make cluster-rescan NODE=node-2   # marks node-2's copies for verification
make cluster-reconcile            # or wait for the reconciler
```

Each cycle verifies up to `NODEMESH_RECONCILE_BATCH` copies (default 100).
Missing or corrupt copies are recorded and re-created from the healthy ones.
Watch `make cluster-status` until nothing is under-replicated.

### Corrupt data on a node

Detection is automatic:
- reads verify the SHA-256 and fall back to another copy;
- the scrub re-hashes copies periodically.

Corrupt copies are deleted and re-created. Nothing to do except investigate
the disk.

### A node's disk is full

The node refuses writes (`507`) below `NODE_MIN_FREE_BYTES`, and uploads place
the copy elsewhere if possible. Free space or add a node. Existing data is
unaffected.

### The database is unreachable

**Impact.**
- The coordinator turns unhealthy (`/readyz`), and uploads and downloads fail.
- The reconciler's cycles fail, so it turns unhealthy after
  `NODEMESH_RECONCILER_MAX_SILENCE`.

Nothing is lost or written while the database is down. Both services recover
by themselves when it returns.

### The coordinator or reconciler is down

- **Coordinator down:** nothing can be read or written. It is a single point of
  failure (see [consistency.md](consistency.md#what-is-not-guaranteed)).
- **Reconciler down:** service continues, but there is no repair, garbage
  collection or health probing until it is back.

### After a host reboot

The containers restart automatically. Check `make cluster-ps`, then
`make cluster-status`. Node health goes back to `healthy` on the first probe.

## Nodes

### Adding a node

`compose.yaml` defines a spare node, `node-4`, using the same image:

```bash
make cluster-spare-up
docker compose --env-file .env.cluster -f compose.yaml -f compose.external-db.yaml \
  exec coordinator python manage.py register_node node-4 http://node-4:8000
```

For a fifth node:
1. Copy the `node-4` service block, changing `node-5` everywhere, and give it
   its own volume.
2. Add `NODE_5_HMAC_SECRET` to `.env.cluster`
   (`python3 -c "import secrets; print(secrets.token_hex(32))"`).
3. Append `node-5:${NODE_KEY_ID:-k1}:${NODE_5_HMAC_SECRET}` to
   `NODEMESH_NODE_CREDENTIALS` in `compose.yaml`.
4. Run `make cluster-up`, then register the node as above.

New uploads and repairs use new nodes immediately. Existing files are not
rebalanced.

### Draining or disabling a node

```bash
docker compose --env-file .env.cluster -f compose.yaml -f compose.external-db.yaml exec coordinator \
  python manage.py register_node node-2 http://node-2:8000 --state draining   # serves reads, gets no new copies
#                                                          --state disabled   # never contacted
#                                                          --state active     # back to normal
```

Copies on draining or disabled nodes do not count toward durability, so the
reconciler copies those files to other active nodes when one is available.

### Moving a node to a new address

`register_node` refuses to change a registered node's URL. This protects
against a coordinator pointed at the wrong database. Pass `--update-url` when
the move is intended.

## Rotating node keys

- **In the Compose deployment:** change `NODE_<n>_HMAC_SECRET` in
  `.env.cluster`, then run `make cluster-up`. The node, coordinator and
  reconciler restart with the new key; requests in flight during the restart fail.
- **Without downtime:** nodes accept several keys (`NODE_HMAC_KEYS=k2:<new>,k1:<old>`).
  1. Give the node both keys.
  2. Switch the coordinator to `k2`.
  3. Remove `k1` from the node.

  Each step needs a restart. This requires per-node `NODE_HMAC_KEYS` overrides
  in `compose.yaml`.

## Backups

A consistent backup has three parts: the database, the node volumes, and
`.env.cluster` (it holds the keys). Objects are immutable, so this order gives
a usable set without stopping the cluster:

1. **Database first:**
   ```bash
   docker exec <postgres-container> pg_dump -U nodemesh -Fc nodemesh > nodemesh-$(date +%F).dump
   ```
2. **Then each node volume.** Files uploaded after the dump are extra copies
   the restored database does not know about. Files deleted in between may be
   missing.
   ```bash
   for n in 1 2 3; do
     docker run --rm -v <project>_node-$n-data:/data:ro -v "$PWD":/backup alpine \
       tar czf /backup/node-$n-$(date +%F).tgz -C /data objects
   done
   ```
3. **Store the backup files outside the host**, and `.env.cluster` in a secret store.

**Restoring:**
1. Restore the database and the volumes from the same backup set.
2. Start the cluster.
3. Run `make cluster-rescan NODE=node-N` for each node.

If orphan cleanup reports `orphan_gc_refused`, the database and the volumes do
not belong together: stop and check before changing any setting.

**Partial backups.** With three copies, one node's volume can be rebuilt by
the cluster itself (see [Failure scenarios](#a-nodes-volume-is-lost-or-replaced)).
Backups protect against losing the host, the disk, or the database.

## Logs

Every service writes one JSON object per line to stdout:

```bash
make cluster-logs                     # all services
make cluster-logs SERVICE=coordinator
```

The most useful fields are `file_id`, `node_id`, `error` and `report`. Each
container keeps at most 3 × 10 MB of logs (`x-logging` in `compose.yaml`); ship
them elsewhere if you need a longer history.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Build fails with an image tag "already exists" or a similar conflict | Several services built the same image concurrently (fixed: only `coordinator` and `node-1` build). Pull the latest code and run `make cluster-build` |
| `up --wait` fails: a container "has no healthcheck configured" | The reconciler used to disable its healthcheck (fixed: heartbeat healthcheck). Pull, `make cluster-build`, `make cluster-up` |
| `cluster-up`: "pull access denied" / "No such image" for `nodemesh/...` | Images not built yet: `make cluster-build` |
| The coordinator exits: "database schema is not up to date, or the database is unreachable" | Run `make cluster-migrate-plan` and `make cluster-migrate`. Otherwise check `POSTGRES_*`, `POSTGRES_DOCKER_NETWORK` and that the PostgreSQL container is running |
| `cluster-config` or `cluster-up`: "set POSTGRES_… in .env.cluster" | A required value is still `<fill-in>` or missing |
| "network … declared as external, but could not be found" | `POSTGRES_DOCKER_NETWORK` does not match the PostgreSQL container's network (`docker inspect`, see deployment) |
| The reconciler stays `unhealthy` | `make cluster-logs SERVICE=reconciler`: cycles are failing, usually because the database is unreachable |
| Uploads return 503 `replication_quorum_not_met` | Fewer than W nodes are active and not suspect: `make cluster-status`. Check node logs; `bad_signature` in a node's `last_error` means a key mismatch between `.env.cluster` and the running containers (run `make cluster-up` after editing secrets) |
| A node stays `suspect` after being fixed | It returns to `healthy` at the next probe: `make cluster-reconcile` |
| `expired` signature errors | The clocks of the hosts differ by more than 60 s: enable NTP |
| `register_node`: "refusing to change the URL" | The coordinator uses a database that already registered this node elsewhere. Check the database first, then use `--update-url` only if the move is intended |
| `orphan_gc_refused` in the reconciler log | Most objects on a node are unknown to the database: likely a wrong, empty or restored database. Do not raise `NODEMESH_ORPHAN_GC_MAX_FRACTION` before understanding why |
| The cluster comes up empty after moving the checkout | The project name, and therefore the volume names, changed. `make cluster-volumes` lists the old volumes. Set `COMPOSE_PROJECT_NAME` to the old prefix |
