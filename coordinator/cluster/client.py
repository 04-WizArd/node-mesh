"""HTTP client for the storage-node internal API (v1).

Every request is signed (``nodemesh_common.signing``), bounded by explicit
connect/read timeouts, and translated into one of two error families:

* :class:`NodeUnavailable` - the node could not be reached or failed
  internally (connection refused, timeout, 5xx). Possibly transient.
* :class:`NodeRejected` - the node answered and refused the request (4xx,
  507). The node itself is alive.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

import requests
from django.conf import settings
from requests.adapters import HTTPAdapter

from nodemesh_common.signing import EMPTY_SHA256, Credential, sign_request

from .models import StorageNode

API_PREFIX = "/internal/v1"
_local = threading.local()


class NodeError(Exception):
    def __init__(self, node_id: str, code: str, message: str = "") -> None:
        super().__init__(f"{node_id}: {code}{' - ' + message if message else ''}")
        self.node_id = node_id
        self.code = code


class NodeUnavailable(NodeError):
    pass


class NodeRejected(NodeError):
    def __init__(self, node_id: str, code: str, status: int, message: str = "") -> None:
        super().__init__(node_id, code, message)
        self.status = status


class NodeMisconfigured(NodeError):
    pass


@dataclass(frozen=True)
class StoredObject:
    object_id: uuid.UUID
    size: int
    sha256: str
    created: bool = False


def _session() -> requests.Session:
    session = getattr(_local, "session", None)
    if session is None:
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=16, pool_maxsize=16, max_retries=0)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        _local.session = session
    return session


class NodeClient:
    def __init__(
        self,
        node_id: str,
        base_url: str,
        credential: Credential,
        *,
        connect_timeout: float,
        read_timeout: float,
        verify: bool | str = True,
        chunk_size: int = 256 * 1024,
    ) -> None:
        self.node_id = node_id
        self.base_url = base_url.rstrip("/")
        self.credential = credential
        self.timeout = (connect_timeout, read_timeout)
        self.verify = verify
        self.chunk_size = chunk_size

    # -- plumbing -------------------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: Any = None,
        content_sha256: str = EMPTY_SHA256,
        content_length: int = 0,
        stream: bool = False,
    ) -> requests.Response:
        full_path = API_PREFIX + path
        query_string = urlencode(query or {})
        headers = sign_request(
            self.credential,
            node_id=self.node_id,
            method=method,
            path=full_path,
            query=query_string,
            content_sha256=content_sha256,
            content_length=content_length,
        )
        if body is not None:
            headers["Content-Type"] = "application/octet-stream"
            headers["Content-Length"] = str(content_length)
        url = self.base_url + full_path + (f"?{query_string}" if query_string else "")
        try:
            response = _session().request(
                method,
                url,
                headers=headers,
                data=body,
                timeout=self.timeout,
                stream=stream,
                verify=self.verify,
                allow_redirects=False,
            )
        except requests.Timeout as exc:
            raise NodeUnavailable(self.node_id, "timeout", type(exc).__name__) from exc
        except requests.RequestException as exc:
            raise NodeUnavailable(self.node_id, "connection_error", type(exc).__name__) from exc

        if response.status_code >= 400:
            code = _error_code(response)
            response.close()
            if response.status_code >= 500 and response.status_code != 507:
                raise NodeUnavailable(self.node_id, code, f"HTTP {response.status_code}")
            raise NodeRejected(self.node_id, code, response.status_code)
        return response

    # -- operations -----------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        with self._request("GET", "/status") as response:
            return _json(self.node_id, response)

    def put_object(self, object_id: uuid.UUID, path: Path, size: int, sha256: str) -> StoredObject:
        """Upload a local file. Success means the node fsynced and verified it."""
        with open(path, "rb") as handle:
            # requests switches to chunked encoding for an empty file object,
            # which nodes refuse; an empty bytes body gets "Content-Length: 0".
            body = handle if size else b""
            with self._request(
                "PUT",
                f"/objects/{object_id}",
                body=body,
                content_sha256=sha256,
                content_length=size,
            ) as response:
                payload = _json(self.node_id, response)
        if payload.get("sha256") != sha256 or payload.get("size") != size or not payload.get("durable"):
            raise NodeUnavailable(self.node_id, "bad_acknowledgement", "node acknowledged different content")
        return StoredObject(object_id, size, sha256, created=response.status_code == 201)

    def head_object(self, object_id: uuid.UUID) -> StoredObject | None:
        try:
            with self._request("HEAD", f"/objects/{object_id}") as response:
                return StoredObject(
                    object_id,
                    int(response.headers.get("X-NodeMesh-Object-Size", "-1")),
                    response.headers.get("X-NodeMesh-Content-SHA256", ""),
                )
        except NodeRejected as exc:
            if exc.status == 404:
                return None
            raise

    @contextmanager
    def get_object(self, object_id: uuid.UUID) -> Iterator[Iterator[bytes]]:
        """Yield an iterator over the object's bytes (raises NodeRejected 404 if absent)."""
        response = self._request("GET", f"/objects/{object_id}", stream=True)
        try:
            yield _iter_body(self.node_id, response, self.chunk_size)
        finally:
            response.close()

    def delete_object(self, object_id: uuid.UUID) -> None:
        self._request("DELETE", f"/objects/{object_id}").close()

    def check_integrity(self, object_id: uuid.UUID) -> dict[str, Any]:
        with self._request("GET", f"/objects/{object_id}/integrity") as response:
            return _json(self.node_id, response)

    def list_objects(self, after: str | None = None, limit: int = 500) -> tuple[list[dict], str | None]:
        query = {"limit": str(limit)}
        if after:
            query["after"] = after
        with self._request("GET", "/objects", query=query) as response:
            payload = _json(self.node_id, response)
        return payload["objects"], payload.get("next_after")


def _iter_body(node_id: str, response: requests.Response, chunk_size: int) -> Iterator[bytes]:
    try:
        yield from response.iter_content(chunk_size=chunk_size)
    except requests.RequestException as exc:  # timeout or reset mid-stream
        raise NodeUnavailable(node_id, "transfer_interrupted", type(exc).__name__) from exc


def _json(node_id: str, response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise NodeUnavailable(node_id, "bad_response", "invalid JSON") from exc
    if not isinstance(payload, dict):
        raise NodeUnavailable(node_id, "bad_response", "unexpected JSON")
    return payload


def _error_code(response: requests.Response) -> str:
    try:
        payload = response.json()
        code = payload.get("detail") or payload.get("error")
        if isinstance(code, str) and code:
            return code[:64]
    except ValueError:
        pass
    return f"http_{response.status_code}"


class ClientFactory(Protocol):
    def __call__(self, node: StorageNode) -> NodeClient: ...


def client_for(node: StorageNode) -> NodeClient:
    """Default factory: build a client from settings for a registered node."""
    credential = settings.NODEMESH_NODE_CREDENTIALS.get(node.node_id)
    if credential is None:
        raise NodeMisconfigured(node.node_id, "missing_credentials", "no key configured for node")
    if urlsplit(node.base_url).path not in ("", "/"):
        raise NodeMisconfigured(node.node_id, "bad_base_url", "base URL must not contain a path")
    return NodeClient(
        node.node_id,
        node.base_url,
        credential,
        connect_timeout=settings.NODEMESH_NODE_CONNECT_TIMEOUT,
        read_timeout=settings.NODEMESH_NODE_READ_TIMEOUT,
        verify=settings.NODEMESH_NODE_CA_BUNDLE or True,
        chunk_size=settings.NODEMESH_IO_CHUNK_BYTES,
    )
