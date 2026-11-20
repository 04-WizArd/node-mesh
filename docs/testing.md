# Testing

```bash
make test        # shared + storage-node + coordinator suites
make test-fast   # coordinator without real node processes
make lint check  # ruff, Django system checks, migration drift
```

## Isolation from the development database

`coordinator/config/test_settings.py` ignores `POSTGRES_*` and the repository
`.env` entirely. The test database is chosen as follows:

- `TEST_POSTGRES_*` set (CI, or a throwaway PostgreSQL): that server, where
  pytest-django creates and drops its own test database;
- otherwise: a temporary SQLite file.

The tests can therefore never create, migrate or drop anything on the Dell's
`distributed_storage`.

## Suites

| Suite | What it exercises |
|---|---|
| `shared/tests` | HMAC signing: tampering of every signed field, expiry, wrong keys, rotation, malformed credentials, query canonicalisation, secret redaction |
| `storage-node/tests/test_blobstore.py` | Byte-exact binary round trip; empty objects; digest mismatch; truncated, interrupted and over-long bodies; disk full (ENOSPC); free-space floor; idempotent PUT and conflicts; 8 concurrent writers of one key; delete idempotency; bit-rot detection; stale-temp cleanup; ordered listing; path containment |
| `storage-node/tests/test_api.py` | Full HTTP lifecycle; unsigned, wrong-key, wrong-node, expired and **replayed** requests; tampered body under a valid signature; chunked encoding; size limit (413); insufficient storage (507); traversal URLs; pagination; integrity endpoint; probes |
| `coordinator/tests/test_rules.py` | Quorum and under-replication rules; database constraints (idempotency, status, one replica per node); replica counting; placement filters and ordering; health transitions; filename and content-type sanitisation; bounded spooling |
| `coordinator/tests/test_web_security.py` | 401 on every API route; other users' files return 404; CSRF on upload, delete and logout; oversized, chunked and multipart uploads; login redirect (no tokens); POST-only logout; registration and password policy; security headers and CSP; no HTML sinks in JS; no inline scripts or handlers in templates |
| `coordinator/tests/test_cluster.py` (`-m cluster`) | Runs against **real storage-node processes** (gunicorn, 2 workers each, real HMAC, real disks), see below |

The `test_cluster.py` scenarios are:
- 1 MiB+ byte-exact round trip on 3 nodes;
- empty file; malicious filename;
- one node down (quorum met, under-replicated);
- two nodes down (503, cleanup, retry with the same key);
- hung node behind a TCP black hole (bounded by timeouts);
- wrong node key;
- read fallback; corrupt replica skipped then repaired;
- scrub finding silent corruption;
- all replicas down (503); restart persistence;
- idempotent replay and conflict;
- concurrent uploads and concurrent duplicate keys;
- re-replication to a spare after a node turns suspect, then recovery;
- coordinator crash resolved from node state;
- orphan deletion and lost-acknowledgement adoption;
- delete and purge; reconciler idempotency.

## Docker smoke test

`scripts/cluster_demo.py` drives a running Compose stack:
- upload a file;
- `docker compose stop node-2`;
- verified download from a surviving replica;
- upload at quorum during the outage;
- restart node-2 and run reconcile;
- check the file is back to 3/3 replicas.

CI runs it in the `docker-smoke` job against `compose.test.yaml`.

## Benchmark

`scripts/benchmark.py` measures upload and verified-download throughput through
the public API, for chosen sizes and concurrency. See the README for measured
numbers and their caveats.
