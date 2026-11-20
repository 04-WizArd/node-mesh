# Consistency, durability and failure semantics

This page states exactly what NodeMesh guarantees, under which assumptions,
and what it does not. Every guarantee listed here has an automated test (named
in brackets, in `coordinator/tests/` or `storage-node/tests/`).

Parameters: replication factor **RF** (default 3), write quorum **W**
(default 2), `NODEMESH_SUSPECT_AFTER_FAILURES` (default 3).

## What "stored" means

A storage node answers `201`/`200` to a PUT only after it has:

- received exactly `Content-Length` bytes;
- verified their SHA-256 against the digest in the signed request;
- `fsync`ed the data file, the metadata file and the temporary directory;
- atomically renamed the directory into place and `fsync`ed the parent.

The coordinator also checks that the acknowledgement echoes the expected digest
and size before marking the replica `stored`.

On macOS `fsync` does not force the drive cache. The production target is Linux.

## Upload outcome

| Situation | HTTP | File status | Notes |
|---|---|---|---|
| ≥ RF copies stored | 201 | `available` | `under_replicated: false` [`test_round_trip_replicates_to_three_nodes_byte_for_byte`] |
| W ≤ copies < RF | 201 | `available` | `under_replicated: true`; the reconciler adds copies later [`test_one_node_down_still_meets_quorum_and_reports_under_replication`] |
| copies < W | 503 `replication_quorum_not_met` | `failed` | Partial copies are deleted by the reconciler; the idempotency key is released [`test_insufficient_quorum_fails_explicitly_and_is_cleaned_up`] |
| fewer than W eligible nodes | 503 | `failed` | No bytes are sent |
| hung node | 201/503 | — | Bounded by connect/read timeouts and attempts; transfers run in parallel [`test_hung_node_is_bounded_by_timeouts`] |
| coordinator crash mid-upload | — | `pending` → resolved | The reconciler asks the nodes and finalises by the same quorum rule [`test_interrupted_upload_is_resolved_from_node_state`] |

A successful quorum is **not** full replication. The API reports both
`stored` and `under_replicated`.

### Idempotency

The optional `Idempotency-Key` header is unique per user.

- Same key and same content: the original result is returned (`200`, `replayed: true`).
- Same key and different content: `409`.
- Same key while the first upload is still running: `409` with `Retry-After`.
- A `failed` upload releases its key, so a retry runs again.

[`test_duplicate_request_with_same_key_is_replayed`, `test_concurrent_uploads_are_isolated_and_intact`]

## Read guarantees

- A download returns bytes whose size and SHA-256 equal the values recorded at
  upload, or an error. It never returns unverified content.
  [`test_corrupt_replica_is_detected_skipped_and_repaired`]
- A read succeeds as long as **one** stored replica is on a reachable node
  that is not disabled and holds correct bytes.
  [`test_read_falls_back_when_a_replica_node_is_down`, `test_all_replicas_unavailable_returns_503`]

## Consistency model

Metadata lives in a single PostgreSQL database and changes in transactions, so
metadata reads are linearizable. Object content is immutable and every
copy is checked against the SHA-256 recorded at upload. Any replica that passes
verification is therefore *the* content, and there are no stale or conflicting
versions to reconcile.

A read after a successful upload (201) by the same or another client sees the
file, because it is `available` in PostgreSQL before the 201 is sent. The read
succeeds if any of its ≥ W verified copies is reachable.

This does **not** come from a W + R > N quorum argument. Reads use R = 1 with
end-to-end verification against the digest stored in the authoritative
metadata, and that digest is what makes R = 1 safe.

## Fault tolerance (RF = 3, W = 2, three nodes)

| Failed nodes | Uploads | Reads of existing files |
|---|---|---|
| 0 | accepted, 3/3 | yes |
| 1 | accepted, 2/3, repaired later | yes |
| 2 | rejected (503) | yes, if the surviving node holds a copy |
| 3 | rejected | no |

With a fourth (spare) node, the reconciler restores RF after a node becomes
`suspect` [`test_failed_node_triggers_re_replication_to_a_spare`].

## Recovery

- **Missing replicas** are re-created from a verified copy.
- **Corrupt replicas** are found on read or by the periodic scrub, deleted and re-created.
  [`test_scrub_finds_silent_corruption_without_any_read`]
- **Lost acknowledgements** (a write that timed out but succeeded) are adopted
  when the node lists the object with the correct digest.
- **Orphans** (objects with no metadata) are deleted after a grace period.
  [`test_orphans_are_deleted_and_lost_acknowledgements_adopted`]
- **Restarted nodes** keep their data and return to `healthy` on the next probe.
  [`test_data_survives_node_restart`]
- **Interrupted node writes** live in `tmp/` and are removed at node start.

Every step is idempotent. [`test_reconcile_is_idempotent`]

## What is NOT guaranteed

- **Coordinator and database availability.** Both are single points of
  failure. If the coordinator is down, nothing is readable or writable. Node
  redundancy does not change this.
- **Durability against correlated failures.** All nodes on one host (the
  Compose setup) share a disk, power supply and kernel. Real independence
  requires nodes on separate machines.
- **Immediate re-replication.** Repair happens on the next reconciler cycle
  after a node is declared `suspect`, so data is under-replicated for that
  window.
- **Over-replication cleanup.** When a suspect node comes back, files that were
  repaired in the meantime have RF+1 copies. Extra copies are kept, not trimmed.
- **Capacity growth by adding nodes.** Each file still needs RF full copies.
  Extra nodes give placement more choice and more total space, but never more
  space for a single object than its smallest target has.
- **Byzantine nodes.** A malicious node cannot make the coordinator serve bad
  bytes, because the digest is checked. It can, however, refuse service or lie
  about free space.
