from __future__ import annotations

import os
import time

import pytest

from nodemesh_common import signing
from objects.apps import get_store

from .conftest import new_id, sha, signed_headers

BODY = os.urandom(70_000)


def obj_path(oid) -> str:
    return f"/internal/v1/objects/{oid}"


def put(client, oid, body=BODY, **sign_kwargs):
    path = obj_path(oid)
    headers = signed_headers("PUT", path, body=body, **sign_kwargs)
    return client.generic("PUT", path, data=body, content_type="application/octet-stream", headers=headers)


def get(client, path, method="GET", query=""):
    headers = signed_headers(method, path, query=query)
    url = f"{path}?{query}" if query else path
    return client.generic(method, url, headers=headers)


def test_put_get_head_delete_lifecycle(client):
    oid = new_id()
    created = put(client, oid)
    assert created.status_code == 201
    assert created.json()["sha256"] == sha(BODY) and created.json()["durable"] is True

    again = put(client, oid)
    assert again.status_code == 200 and again.json()["outcome"] == "already_exists"

    download = get(client, obj_path(oid))
    assert download.status_code == 200
    assert b"".join(download.streaming_content) == BODY
    assert download["X-NodeMesh-Content-SHA256"] == sha(BODY)
    assert download["Content-Type"] == "application/octet-stream"

    head = get(client, obj_path(oid), method="HEAD")
    assert head.status_code == 200 and head["X-NodeMesh-Object-Size"] == str(len(BODY))

    assert get(client, obj_path(oid), method="DELETE").status_code == 204
    assert get(client, obj_path(oid)).status_code == 404
    assert get(client, obj_path(oid), method="DELETE").status_code == 204


def test_conflicting_content_for_same_id_is_rejected(client):
    oid = new_id()
    assert put(client, oid, body=b"first").status_code == 201
    response = put(client, oid, body=b"other")
    assert response.status_code == 409
    assert response.json()["error"] == "object_conflict"


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "HEAD"])
def test_unsigned_requests_are_rejected(client, method):
    response = client.generic(method, obj_path(new_id()), data=b"x" if method == "PUT" else b"")
    assert response.status_code == 401


def test_status_and_listing_require_authentication(client):
    assert client.get("/internal/v1/status").status_code == 401
    assert client.get("/internal/v1/objects").status_code == 401


def test_wrong_key_is_rejected(client):
    rogue = signing.Credential(key_id="test-key", secret=bytes.fromhex("22" * 32))
    response = put(client, new_id(), credential=rogue)
    assert response.status_code == 401
    assert response.json()["detail"] == "bad_signature"


def test_request_signed_for_another_node_is_rejected(client):
    response = put(client, new_id(), node_id="node-other")
    assert response.status_code == 401


def test_expired_request_is_rejected(client):
    response = put(client, new_id(), now=time.time() - 3600)
    assert response.status_code == 401
    assert response.json()["detail"] == "expired"


def test_tampered_body_is_rejected_and_not_stored(client):
    """A valid signature over digest(A) must not allow storing body B."""
    oid = new_id()
    path = obj_path(oid)
    original = b"A" * 1000
    tampered = b"B" * 1000  # same length, different content
    headers = signed_headers("PUT", path, body=original)
    response = client.generic(
        "PUT", path, data=tampered, content_type="application/octet-stream", headers=headers
    )
    assert response.status_code == 422
    assert response.json()["error"] == "digest_mismatch"
    assert get_store().stat(oid) is None


def test_replayed_request_is_rejected(client):
    oid = new_id()
    path = obj_path(oid)
    headers = signed_headers("DELETE", path)
    assert client.generic("DELETE", path, headers=headers).status_code == 204
    replay = client.generic("DELETE", path, headers=headers)
    assert replay.status_code == 401
    assert replay.json()["detail"] == "replayed_request"


def test_body_on_non_put_request_is_rejected(client):
    path = obj_path(new_id())
    headers = signed_headers("GET", path, body=b"surprise")
    response = client.generic("GET", path, data=b"surprise", headers=headers)
    assert response.status_code == 400


def test_chunked_transfer_is_rejected(client):
    path = obj_path(new_id())
    headers = signed_headers("PUT", path, body=b"x")
    response = client.generic("PUT", path, data=b"x", headers={**headers, "Transfer-Encoding": "chunked"})
    assert response.status_code == 411


def test_object_size_limit(client, settings):
    settings.NODE_MAX_OBJECT_BYTES = 1000
    get_store.cache_clear()
    response = put(client, new_id(), body=b"x" * 1001)
    assert response.status_code == 413


def test_insufficient_space_returns_507(client, settings):
    settings.NODE_MIN_FREE_BYTES = 10**18
    get_store.cache_clear()
    assert put(client, new_id(), body=b"x").status_code == 507


def test_malformed_and_traversal_paths_never_reach_storage(client):
    for path in (
        "/internal/v1/objects/../../etc/passwd",
        "/internal/v1/objects/not-a-uuid",
        "/internal/v1/objects/%2e%2e%2fsecret",
    ):
        assert client.get(path).status_code == 404


def test_listing_paginates(client):
    ids = sorted((new_id() for _ in range(3)), key=lambda u: u.hex)
    for oid in ids:
        assert put(client, oid, body=b"x").status_code == 201
    first = get(client, "/internal/v1/objects", query="limit=2").json()
    assert [o["object_id"] for o in first["objects"]] == [str(i) for i in ids[:2]]
    second = get(client, "/internal/v1/objects", query=f"limit=2&after={first['next_after']}").json()
    assert [o["object_id"] for o in second["objects"]] == [str(ids[2])]
    assert second["next_after"] is None


def test_integrity_endpoint_detects_corruption(client):
    oid = new_id()
    put(client, oid, body=b"pristine")
    assert get(client, obj_path(oid) + "/integrity").json()["ok"] is True
    (get_store().object_dir(oid) / "data").write_bytes(b"corrupt!")
    report = get(client, obj_path(oid) + "/integrity").json()
    assert report["ok"] is False and report["actual_sha256"] == sha(b"corrupt!")


def test_status_reports_identity_and_capacity(client):
    body = get(client, "/internal/v1/status").json()
    assert body["node_id"] == "node-test"
    assert body["disk_free_bytes"] > 0


def test_probes_are_public_and_minimal(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    ready = client.get("/readyz")
    assert ready.status_code == 200 and ready.json() == {"status": "ready"}
