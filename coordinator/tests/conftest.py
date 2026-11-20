from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import pytest
import requests
from django.contrib.auth import get_user_model
from django.test import Client

from cluster.models import StorageNode
from config.test_settings import TEST_NODE_SECRETS

REPO = Path(__file__).resolve().parents[2]
NODE_PROJECT = REPO / "storage-node"
UPLOAD_URL = "/api/v1/files/"


# --- users and clients -----------------------------------------------------------
@pytest.fixture
def user(db):
    return get_user_model().objects.create_user("alice", password="correct-horse-battery")


@pytest.fixture
def other_user(db):
    return get_user_model().objects.create_user("mallory", password="correct-horse-battery")


@pytest.fixture
def api(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


def upload(client: Client, data: bytes, name: str = "report.bin", key: str | None = None, **headers):
    extra = {"X-Filename": quote(name)}
    if key:
        extra["Idempotency-Key"] = key
    extra.update(headers)
    return client.generic(
        "POST", UPLOAD_URL, data=data, content_type="application/octet-stream", headers=extra
    )


def download(client: Client, file_id: str):
    return client.get(f"{UPLOAD_URL}{file_id}/content")


def body(response) -> bytes:
    return b"".join(response.streaming_content)


# --- real storage-node processes ----------------------------------------------------
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class NodeProcess:
    """A real storage node (gunicorn, 2 workers) on its own port and data directory."""

    def __init__(self, node_id: str, root: Path) -> None:
        self.node_id = node_id
        self.port = _free_port()
        self.data_dir = root / node_id
        self.proc: subprocess.Popen | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        if self.running:
            return
        env = {
            **os.environ,
            "NODE_ID": self.node_id,
            "NODE_DATA_DIR": str(self.data_dir),
            "NODE_HMAC_KEYS": f"test-key:{TEST_NODE_SECRETS[self.node_id]}",
            "NODE_MIN_FREE_BYTES": "0",
            "DJANGO_ALLOWED_HOSTS": "127.0.0.1,localhost",
            "DJANGO_SETTINGS_MODULE": "config.settings",
            "LOG_LEVEL": "WARNING",
        }
        env.pop("NODE_ENV_FILE", None)
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "gunicorn",
                "config.wsgi:application",
                "--bind",
                f"127.0.0.1:{self.port}",
                "--workers",
                "2",
                "--threads",
                "2",
                "--timeout",
                "30",
                "--graceful-timeout",
                "2",
                "--log-level",
                "warning",
            ],
            cwd=NODE_PROJECT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"{self.node_id} exited: {self.proc.stderr.read().decode()}")
            try:
                if requests.get(f"{self.url}/readyz", timeout=0.5).status_code == 200:
                    return
            except requests.RequestException:
                time.sleep(0.1)
        raise RuntimeError(f"{self.node_id} did not become ready")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def object_path(self, file_id) -> Path:
        key = uuid.UUID(str(file_id)).hex
        return self.data_dir / "objects" / key[:2] / key[2:4] / key / "data"

    def has(self, file_id) -> bool:
        return self.object_path(file_id).is_file()


class BlackHole:
    """Accepts TCP connections and never answers: models a hung node."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}"

    def close(self) -> None:
        self.sock.close()


@pytest.fixture(scope="session")
def node_processes():
    root = Path(tempfile.mkdtemp(prefix="nodemesh-cluster-test-"))
    procs = {nid: NodeProcess(nid, root) for nid in ("node-1", "node-2", "node-3", "node-4")}
    try:
        for proc in procs.values():
            proc.start()
        yield procs
    finally:
        for proc in procs.values():
            proc.stop()
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def cluster(node_processes, db):
    """node-1..3 registered and running; node-4 running but not registered."""
    for proc in node_processes.values():
        proc.start()  # restart anything a previous test stopped
    for nid in ("node-1", "node-2", "node-3"):
        StorageNode.objects.create(node_id=nid, base_url=node_processes[nid].url)
    yield node_processes
    for proc in node_processes.values():
        proc.start()


@pytest.fixture
def black_hole():
    hole = BlackHole()
    yield hole
    hole.close()
