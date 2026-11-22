# API reference

There are two APIs:

- the **public API**, served by the coordinator to browsers and clients;
- the **internal API**, served by storage nodes to the coordinator only.

Both use JSON errors of the form `{"error": "<stable_code>", ...}`.

## Public API (coordinator) — `/api/v1/`

Authentication uses a Django session cookie, obtained via `/accounts/login/`.
State-changing requests (`POST`, `DELETE`) must send the CSRF token in
`X-CSRFToken`; the web UI reads it from `<meta name="csrf-token">`.
Unauthenticated requests receive `401 {"error": "authentication_required"}`,
not a redirect.

### `POST /api/v1/files/` — upload

The request body **is** the file (not multipart).

| Header | Required | Meaning |
|---|---|---|
| `Content-Length` | yes (or empty body) | Must be ≤ `NODEMESH_MAX_UPLOAD_BYTES`; the limit is also enforced on bytes actually received |
| `Content-Type` | no | Stored as information; downloads are always served as `application/octet-stream` |
| `X-Filename` | no | Percent-encoded UTF-8 display name. Path components and control characters are stripped; never used as a path |
| `Idempotency-Key` | no | `[A-Za-z0-9._:-]{1,128}`, unique per user (see [consistency](consistency.md#idempotency)) |

| Response | Body |
|---|---|
| `201` | `{"file": File, "replayed": false}`: quorum met |
| `200` | `{"file": File, "replayed": true}`: same idempotency key and same content |
| `400` | `bad_length`, `bad_idempotency_key`, `incomplete_upload` |
| `409` | `idempotency_key_reused` (different content), `upload_in_progress` (+ `Retry-After`) |
| `411` | `length_required` (chunked transfer encoding) |
| `413` | `file_too_large` + `max_bytes` |
| `415` | `raw_body_required` (multipart or urlencoded body) |
| `503` | `replication_quorum_not_met` + `stored`, `required` |

```bash
curl -b cookies.txt -H "X-CSRFToken: $TOKEN" -H "X-Filename: rapport%20final.pdf" \
     -H "Idempotency-Key: $(uuidgen)" -H "Content-Type: application/pdf" \
     --data-binary @rapport.pdf http://127.0.0.1:8080/api/v1/files/
```

### File object

```json
{
  "id": "3f0c…",
  "name": "rapport final.pdf",
  "content_type": "application/pdf",
  "size": 104857,
  "sha256": "9f86d0…",
  "status": "available",
  "created_at": "2026-11-20T17:03:12.512+00:00",
  "replication": {"desired": 3, "stored": 3, "healthy": 3, "under_replicated": false},
  "download_url": "/api/v1/files/3f0c…/content",
  "preview": {"kind": "pdf", "unavailable_reason": null, "url": "/api/v1/files/3f0c…/preview"}
}
```

`preview.kind` (`image`, `pdf`, `text` or `null`) is derived from metadata
only: it tells a client which previews to offer. `unavailable_reason` is
`preview_unsupported` or `preview_too_large` when `kind` is `null`. The server
re-checks the actual bytes on every preview request.

The replication fields mean:
- `stored`: copies marked stored;
- `healthy`: copies on `active`, non-`suspect` nodes (what durability counts);
- `under_replicated`: `healthy < desired`.

### Other endpoints

| Method and path | Description | Responses |
|---|---|---|
| `GET /api/v1/files/?limit=50&offset=0` | Your available files, newest first (`limit` ≤ 200) | `200 {"files": [...], "count", "limit", "offset"}` |
| `GET /api/v1/files/{id}/` | One file | `200 {"file": File}`, `404` |
| `GET /api/v1/files/{id}/content` | Verified download (attachment, `X-Content-SHA256`, `Cache-Control: private, no-store`) | `200`, `404`, `503 no_replica_available` |
| `GET /api/v1/files/{id}/preview` | Verified inline preview (see below) | `200`, `404`, `413 preview_too_large`, `415 preview_unsupported`, `503 no_replica_available` |
| `DELETE /api/v1/files/{id}/` | Delete (copies are purged by the reconciler) | `204`, `404` |
| `GET /api/v1/cluster/nodes/` | Node health and capacity (**staff only**) | `200`, `403` |
| `GET /healthz` | Liveness | `200` |
| `GET /readyz` | Readiness (database reachable) | `200`, `503` |

Files belonging to other users are indistinguishable from files that do not
exist: both return `404`.

### Previews

`GET /api/v1/files/{id}/preview` uses the same authorisation and the same
verified read as the download (spool-and-verify, replica fallback). Only these
formats are ever served inline:

| Format | Detected by | Served as | Default limit |
|---|---|---|---|
| JPEG, PNG, WebP | magic number | `image/jpeg`, `image/png`, `image/webp` | 25 MiB (`NODEMESH_PREVIEW_MAX_BYTES`) |
| PDF | `%PDF-` header | `application/pdf` | 25 MiB |
| Text (`.txt`, `.log`, `.md`, `.csv`, `.json`, `.yaml`, `text/*`...) | valid UTF-8, no NUL byte | `text/plain; charset=utf-8` | 1 MiB (`NODEMESH_PREVIEW_TEXT_MAX_BYTES`) |

How the format is decided:
- The declared type and the extension only nominate a candidate.
- The **bytes decide**: a file declared `image/png` that contains HTML gets `415`.
- HTML, XML and other markup are shown as plain text and are never rendered.
- SVG and every other format return `415`.
- Size limits and unsupported types are rejected from metadata, before any
  storage node is contacted.

Response headers:
- `Content-Disposition: inline`, `X-Content-Type-Options: nosniff`,
  `Cache-Control: private, no-store`, `Cross-Origin-Resource-Policy: same-origin`.
- A response-specific CSP: `default-src 'none'; … sandbox` for images and text.
  For PDF it is a policy without `sandbox`, that allows only the browser's
  built-in viewer and forbids framing. PDFs are opened in their own tab, not
  embedded, because the application forbids framing.

## Internal API (storage node) — `/internal/v1/`

This API is reachable only on the private network and only with a valid
signature. Requests without valid authentication receive `401` with a
`detail` code: `missing_auth`, `unknown_key`, `expired`, `bad_signature` or
`replayed_request`.

| Method and path | Purpose | Responses |
|---|---|---|
| `PUT objects/{uuid}` | Store an object. Raw body; `Content-Length` required; digest in `X-NodeMesh-Content-SHA256` | `201` created, `200` already present (same digest), `400 incomplete_body`, `409 object_conflict`, `411`, `413 object_too_large`, `422 digest_mismatch`, `507 insufficient_storage` |
| `GET objects/{uuid}` | Stream an object; headers `X-NodeMesh-Content-SHA256`, `X-NodeMesh-Object-Size` | `200`, `404` |
| `HEAD objects/{uuid}` | Size and digest only | `200`, `404` |
| `DELETE objects/{uuid}` | Delete (idempotent) | `204` |
| `GET objects/{uuid}/integrity` | Re-hash on disk: `{"ok", "actual_sha256", "actual_size", ...}` | `200`, `404` |
| `GET objects?after={uuid}&limit=500` | Paginated inventory (`limit` ≤ 1000) | `200 {"objects": [...], "next_after"}` |
| `GET status` | `node_id`, disk total/free, limits | `200` |
| `GET /healthz`, `GET /readyz` | Unauthenticated probes; reveal nothing beyond up/ready | `200` / `503` |

A successful PUT response is `{"object_id", "size", "sha256", "stored_at", "outcome", "durable": true}`.

### Request signing (protocol v1)

The headers are:
- `X-NodeMesh-Key-Id`
- `X-NodeMesh-Timestamp` (Unix seconds)
- `X-NodeMesh-Nonce` (32 hex characters)
- `X-NodeMesh-Content-SHA256`
- `X-NodeMesh-Signature`

The signature is `hex(HMAC-SHA256(secret, canonical))`, where `canonical` is:

```
NODEMESH-HMAC-SHA256-V1
<node_id>
<METHOD>
<path>
<query, parameters sorted and percent-encoded>
<timestamp>
<nonce>
<content sha256 hex>
<content length>
```

The node:
1. looks up the key id among its accepted keys (several during rotation);
2. rejects timestamps more than ±60 s from its clock;
3. compares signatures in constant time;
4. records the nonce with `O_EXCL` in its data directory, so every signed
   request is single-use across all worker processes;
5. for PUT, hashes the bytes it actually receives and rejects them (422) if
   they differ from the signed digest. The signature covers the *declared*
   digest, and this check binds it to the *actual* body.

Bodies are not allowed on other methods. Including `node_id` in the canonical
string means a request signed for one node cannot be replayed against another.
Implementation: `shared/src/nodemesh_common/signing.py`.
