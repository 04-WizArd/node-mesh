import json
import logging

import pytest

from nodemesh_common import signing
from nodemesh_common.logging import JsonFormatter

SECRET = "ab" * 32
NODE = "node-1"
PATH = "/internal/v1/objects/0d6c0c7e-3c7f-4b0a-9a38-6a3c0b9b4f10"
DIGEST = "a" * 64


@pytest.fixture
def cred():
    return signing.Credential(key_id="k1", secret=bytes.fromhex(SECRET))


def _verify(cred, headers, **overrides):
    params = dict(
        keys={cred.key_id: cred},
        node_id=NODE,
        method="PUT",
        path=PATH,
        query="",
        headers=headers,
        content_length=10,
        now=1_000_000,
    )
    params.update(overrides)
    return signing.verify_request(**params)


def _sign(cred, **overrides):
    params = dict(
        node_id=NODE,
        method="PUT",
        path=PATH,
        content_sha256=DIGEST,
        content_length=10,
        now=1_000_000,
    )
    params.update(overrides)
    return signing.sign_request(cred, **params)


def test_valid_signature_round_trip(cred):
    verified = _verify(cred, _sign(cred))
    assert verified.key_id == "k1"
    assert verified.content_sha256 == DIGEST


def test_each_signature_uses_a_fresh_nonce(cred):
    assert _sign(cred)[signing.HEADER_NONCE] != _sign(cred)[signing.HEADER_NONCE]


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"method": "DELETE"}, "bad_signature"),
        ({"path": PATH.replace("0d6c", "1d6c")}, "bad_signature"),
        ({"node_id": "node-2"}, "bad_signature"),
        ({"content_length": 11}, "bad_signature"),
        ({"query": "x=1"}, "bad_signature"),
        ({"now": 1_000_000 + 61}, "expired"),
        ({"now": 1_000_000 - 61}, "expired"),
    ],
)
def test_tampering_is_rejected(cred, override, code):
    with pytest.raises(signing.SignatureError) as exc:
        _verify(cred, _sign(cred), **override)
    assert exc.value.code == code


def test_tampered_digest_header_is_rejected(cred):
    headers = _sign(cred)
    headers[signing.HEADER_CONTENT_SHA256] = "b" * 64
    with pytest.raises(signing.SignatureError, match="signature"):
        _verify(cred, headers)


def test_wrong_secret_is_rejected(cred):
    other = signing.Credential(key_id="k1", secret=bytes.fromhex("cd" * 32))
    with pytest.raises(signing.SignatureError) as exc:
        _verify(cred, _sign(other))
    assert exc.value.code == "bad_signature"


def test_unknown_key_and_missing_headers(cred):
    headers = _sign(cred)
    headers[signing.HEADER_KEY_ID] = "k9"
    with pytest.raises(signing.SignatureError) as exc:
        _verify(cred, headers)
    assert exc.value.code == "unknown_key"
    with pytest.raises(signing.SignatureError) as exc:
        _verify(cred, {})
    assert exc.value.code == "missing_auth"


def test_key_rotation_accepts_both_keys(cred):
    new = signing.Credential(key_id="k2", secret=bytes.fromhex("cd" * 32))
    keys = {"k1": cred, "k2": new}
    assert _verify(cred, _sign(new), keys=keys).key_id == "k2"
    assert _verify(cred, _sign(cred), keys=keys).key_id == "k1"


def test_query_canonicalisation_is_order_independent(cred):
    headers = _sign(cred, query="b=2&a=1")
    assert _verify(cred, headers, query="a=1&b=2")


def test_parse_node_credentials():
    creds = signing.parse_node_credentials(f"node-1:k1:{SECRET}, node-2:k1:{SECRET}")
    assert set(creds) == {"node-1", "node-2"}
    assert "redacted" in repr(creds["node-1"]) and SECRET not in repr(creds["node-1"])


@pytest.mark.parametrize(
    "value",
    ["node-1:k1", "Node_1:k1:" + SECRET, "node-1:k1:zz", "node-1:k1:abcd", f"n:k:{SECRET},n:k:{SECRET}"],
)
def test_parse_node_credentials_rejects_malformed(value):
    with pytest.raises(signing.SigningConfigError):
        signing.parse_node_credentials(value)


def test_parse_key_list():
    keys = signing.parse_key_list(f"k2:{SECRET},k1:{SECRET}")
    assert list(keys) == ["k2", "k1"]


def test_json_formatter_includes_extra_fields():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    record.node_id = "node-1"
    payload = json.loads(JsonFormatter(service="test").format(record))
    assert payload["msg"] == "hello world"
    assert payload["node_id"] == "node-1"
    assert payload["service"] == "test"
