"""HMAC-SHA256 authentication for coordinator -> storage-node requests (protocol v1).

Every internal request carries five headers::

    X-NodeMesh-Key-Id          identifier of the shared key used (enables rotation)
    X-NodeMesh-Timestamp       Unix time in seconds when the request was signed
    X-NodeMesh-Nonce           128-bit random value, single use
    X-NodeMesh-Content-SHA256  hex SHA-256 of the request body (of b"" when empty)
    X-NodeMesh-Signature       hex HMAC-SHA256 over the canonical request

The canonical request binds the target node, the method, the path, the
normalised query string, the timestamp, the nonce, the declared body digest and
the declared body length. The signature therefore authenticates the *declared*
digest only: a receiver MUST hash the bytes it actually receives and reject the
request when they differ from ``X-NodeMesh-Content-SHA256`` (the storage node
does this while streaming the body to disk).

Replay protection needs state (seen nonces), so it is left to the receiver;
this module only validates the freshness window.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote

SCHEME = "NODEMESH-HMAC-SHA256-V1"

HEADER_KEY_ID = "X-NodeMesh-Key-Id"
HEADER_TIMESTAMP = "X-NodeMesh-Timestamp"
HEADER_NONCE = "X-NodeMesh-Nonce"
HEADER_CONTENT_SHA256 = "X-NodeMesh-Content-SHA256"
HEADER_SIGNATURE = "X-NodeMesh-Signature"

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
DEFAULT_MAX_SKEW_SECONDS = 60
MIN_SECRET_BYTES = 32

_KEY_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_NODE_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


class SigningConfigError(ValueError):
    """Raised when key material or identifiers are malformed."""


class SignatureError(Exception):
    """Raised when an incoming request fails authentication.

    ``code`` is a stable, non-sensitive identifier suitable for logs and
    error responses.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Credential:
    key_id: str
    secret: bytes

    def __post_init__(self) -> None:
        if not _KEY_ID_RE.match(self.key_id):
            raise SigningConfigError("invalid key id")
        if len(self.secret) < MIN_SECRET_BYTES:
            raise SigningConfigError(f"secrets must be at least {MIN_SECRET_BYTES} bytes")

    def __repr__(self) -> str:  # never leak the secret through logs or tracebacks
        return f"Credential(key_id={self.key_id!r}, secret=<redacted>)"


@dataclass(frozen=True)
class VerifiedRequest:
    key_id: str
    nonce: str
    timestamp: int
    content_sha256: str


def is_valid_node_id(value: str) -> bool:
    return bool(_NODE_ID_RE.match(value))


def is_valid_sha256(value: str) -> bool:
    return bool(_SHA256_RE.match(value))


def _parse_credential(key_id: str, secret_hex: str) -> Credential:
    if not secret_hex or not _HEX_RE.match(secret_hex) or len(secret_hex) % 2:
        raise SigningConfigError(f"secret for key {key_id!r} must be hex-encoded")
    return Credential(key_id=key_id, secret=bytes.fromhex(secret_hex))


def parse_key_list(value: str) -> dict[str, Credential]:
    """Parse ``"key-id:hexsecret,key-id2:hexsecret2"`` (a node's accepted keys)."""
    keys: dict[str, Credential] = {}
    for item in filter(None, (part.strip() for part in value.split(","))):
        key_id, sep, secret_hex = item.partition(":")
        if not sep:
            raise SigningConfigError("expected 'key-id:hexsecret'")
        if key_id in keys:
            raise SigningConfigError(f"duplicate key id {key_id!r}")
        keys[key_id] = _parse_credential(key_id, secret_hex)
    return keys


def parse_node_credentials(value: str) -> dict[str, Credential]:
    """Parse ``"node-1:key-id:hexsecret,node-2:..."`` (the coordinator's signing keys)."""
    creds: dict[str, Credential] = {}
    for item in filter(None, (part.strip() for part in value.split(","))):
        parts = item.split(":")
        if len(parts) != 3:
            raise SigningConfigError("expected 'node-id:key-id:hexsecret'")
        node_id, key_id, secret_hex = parts
        if not is_valid_node_id(node_id):
            raise SigningConfigError(f"invalid node id {node_id!r}")
        if node_id in creds:
            raise SigningConfigError(f"duplicate credentials for node {node_id!r}")
        creds[node_id] = _parse_credential(key_id, secret_hex)
    return creds


def canonical_query(query: str) -> str:
    pairs = sorted(parse_qsl(query, keep_blank_values=True))
    return "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in pairs)


def canonical_request(
    *,
    node_id: str,
    method: str,
    path: str,
    query: str,
    timestamp: int,
    nonce: str,
    content_sha256: str,
    content_length: int,
) -> bytes:
    return "\n".join(
        [
            SCHEME,
            node_id,
            method.upper(),
            path,
            canonical_query(query),
            str(timestamp),
            nonce,
            content_sha256,
            str(content_length),
        ]
    ).encode("utf-8")


def _signature(secret: bytes, message: bytes) -> str:
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def sign_request(
    credential: Credential,
    *,
    node_id: str,
    method: str,
    path: str,
    query: str = "",
    content_sha256: str = EMPTY_SHA256,
    content_length: int = 0,
    now: float | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """Return the authentication headers for one request. Each call uses a fresh nonce."""
    if not is_valid_sha256(content_sha256):
        raise SigningConfigError("content_sha256 must be 64 lowercase hex characters")
    timestamp = int(time.time() if now is None else now)
    nonce = nonce or secrets.token_hex(16)
    message = canonical_request(
        node_id=node_id,
        method=method,
        path=path,
        query=query,
        timestamp=timestamp,
        nonce=nonce,
        content_sha256=content_sha256,
        content_length=content_length,
    )
    return {
        HEADER_KEY_ID: credential.key_id,
        HEADER_TIMESTAMP: str(timestamp),
        HEADER_NONCE: nonce,
        HEADER_CONTENT_SHA256: content_sha256,
        HEADER_SIGNATURE: _signature(credential.secret, message),
    }


def verify_request(
    *,
    keys: Mapping[str, Credential],
    node_id: str,
    method: str,
    path: str,
    query: str,
    headers: Mapping[str, str],
    content_length: int,
    max_skew_seconds: int = DEFAULT_MAX_SKEW_SECONDS,
    now: float | None = None,
) -> VerifiedRequest:
    """Authenticate an incoming request or raise :class:`SignatureError`.

    ``headers`` must be a case-insensitive mapping (Django's ``request.headers``
    is). The caller remains responsible for nonce replay detection and for
    checking the received body against ``VerifiedRequest.content_sha256``.
    """
    key_id = headers.get(HEADER_KEY_ID, "")
    timestamp_raw = headers.get(HEADER_TIMESTAMP, "")
    nonce = headers.get(HEADER_NONCE, "")
    content_sha256 = headers.get(HEADER_CONTENT_SHA256, "")
    signature = headers.get(HEADER_SIGNATURE, "")

    if not (key_id and timestamp_raw and nonce and content_sha256 and signature):
        raise SignatureError("missing_auth", "authentication headers are missing")
    credential = keys.get(key_id)
    if credential is None:
        raise SignatureError("unknown_key", "unknown key id")
    if not timestamp_raw.isdigit():
        raise SignatureError("bad_timestamp", "malformed timestamp")
    if not _NONCE_RE.match(nonce):
        raise SignatureError("bad_nonce", "malformed nonce")
    if not is_valid_sha256(content_sha256):
        raise SignatureError("bad_digest", "malformed content digest")

    timestamp = int(timestamp_raw)
    current = time.time() if now is None else now
    if abs(current - timestamp) > max_skew_seconds:
        raise SignatureError("expired", "timestamp outside the accepted window")

    expected = _signature(
        credential.secret,
        canonical_request(
            node_id=node_id,
            method=method,
            path=path,
            query=query,
            timestamp=timestamp,
            nonce=nonce,
            content_sha256=content_sha256,
            content_length=content_length,
        ),
    )
    if not hmac.compare_digest(expected, signature.lower()):
        raise SignatureError("bad_signature", "signature mismatch")
    return VerifiedRequest(key_id=key_id, nonce=nonce, timestamp=timestamp, content_sha256=content_sha256)


def generate_secret_hex() -> str:
    return secrets.token_hex(MIN_SECRET_BYTES)
