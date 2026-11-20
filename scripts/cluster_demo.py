#!/usr/bin/env python3
"""End-to-end failure demonstration (also used as the Docker smoke test in CI).

Talks to a running cluster through the public API only, and drives failures
through Docker Compose:

 1. upload a random file (replicated to 3 nodes)
 2. stop node-2
 3. download it again: served from a surviving replica, SHA-256 verified
 4. upload a second file while node-2 is down: accepted at quorum (2/3)
 5. restart node-2
 6. run one reconciliation cycle
 7. the second file is back to 3/3 replicas (re-replicated onto node-2)

Exits non-zero as soon as an expectation is not met. Standard library only.

    python3 scripts/cluster_demo.py --create-user \
        --compose "docker compose --env-file .env.cluster -f compose.yaml -f compose.test.yaml"
"""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
import secrets
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


class DemoFailure(Exception):
    pass


class Api:
    def __init__(self, base_url: str) -> None:
        if urllib.parse.urlsplit(base_url).scheme not in ("http", "https"):
            raise DemoFailure("the URL must start with http:// or https://")
        self.base = base_url.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def _csrf(self) -> str:
        for cookie in self.jar:
            if cookie.name == "csrftoken":
                return cookie.value
        raise DemoFailure("no CSRF cookie")

    def request(self, method: str, path: str, data: bytes | None = None, headers: dict | None = None):
        # The scheme is restricted to http(s) in __init__.
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})  # noqa: S310
        try:
            with self.opener.open(req, timeout=120) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as err:
            return err.code, dict(err.headers), err.read()

    def login(self, username: str, password: str) -> None:
        self.request("GET", "/accounts/login/")
        form = urllib.parse.urlencode(
            {"username": username, "password": password, "csrfmiddlewaretoken": self._csrf()}
        ).encode()
        status, _, _ = self.request(
            "POST",
            "/accounts/login/",
            form,
            {"Content-Type": "application/x-www-form-urlencoded", "Referer": self.base + "/accounts/login/"},
        )
        status, _, _ = self.request("GET", "/api/v1/files/")
        if status != 200:
            raise DemoFailure("login failed")

    def upload(self, name: str, data: bytes) -> tuple[int, dict]:
        status, _, body = self.request(
            "POST",
            "/api/v1/files/",
            data,
            {
                "Content-Type": "application/octet-stream",
                "X-Filename": urllib.parse.quote(name),
                "Idempotency-Key": secrets.token_hex(16),
                "X-CSRFToken": self._csrf(),
                "Referer": self.base + "/",
            },
        )
        return status, json.loads(body or b"{}")

    def download(self, file_id: str) -> tuple[int, bytes]:
        status, _, body = self.request("GET", f"/api/v1/files/{file_id}/content")
        return status, body

    def detail(self, file_id: str) -> dict:
        _, _, body = self.request("GET", f"/api/v1/files/{file_id}/")
        return json.loads(body)["file"]


def step(message: str) -> None:
    print(f"\n==> {message}", flush=True)


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise DemoFailure(message)
    print(f"    ok: {message}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--username", default="demo")
    parser.add_argument("--password", default=os.environ.get("DEMO_PASSWORD"))
    parser.add_argument("--compose", default="docker compose --env-file .env.cluster -f compose.yaml")
    parser.add_argument("--create-user", action="store_true", help="create the demo superuser first")
    parser.add_argument("--size", type=int, default=3 * 1024 * 1024 + 7)
    args = parser.parse_args()

    compose = shlex.split(args.compose)
    password = args.password or secrets.token_urlsafe(18)

    def run(*cmd: str, env: dict | None = None) -> str:
        result = subprocess.run(
            [*compose, *cmd], check=True, capture_output=True, text=True, env={**os.environ, **(env or {})}
        )
        return result.stdout

    try:
        if args.create_user:
            step(f"create user '{args.username}'")
            run(
                "exec", "-T", "-e", "DJANGO_SUPERUSER_PASSWORD", "coordinator",
                "python", "manage.py", "createsuperuser", "--noinput",
                "--username", args.username, "--email", f"{args.username}@example.invalid",
                env={"DJANGO_SUPERUSER_PASSWORD": password},
            )  # fmt: skip
        elif not args.password:
            raise DemoFailure("pass --password (or DEMO_PASSWORD) or use --create-user")

        api = Api(args.url)
        api.login(args.username, password)

        step(f"upload a {args.size} byte file")
        first = os.urandom(args.size)
        status, payload = api.upload("démo-1.bin", first)
        expect(status == 201, f"upload accepted (HTTP {status})")
        file_1 = payload["file"]
        expect(file_1["replication"]["stored"] == 3, "3/3 replicas stored")

        step("stop node-2")
        run("stop", "node-2")

        step("download the first file again")
        status, body = api.download(file_1["id"])
        expect(status == 200, f"download succeeded with node-2 down (HTTP {status})")
        expect(hashlib.sha256(body).hexdigest() == file_1["sha256"], "SHA-256 matches the original")

        step("upload a second file while node-2 is down")
        second = os.urandom(args.size // 3)
        status, payload = api.upload("démo-2.bin", second)
        expect(status == 201, f"upload accepted at write quorum (HTTP {status})")
        file_2 = payload["file"]
        rep = file_2["replication"]
        expect(rep["stored"] == 2 and rep["under_replicated"], "2/3 replicas, flagged under-replicated")

        step("restart node-2")
        run("up", "-d", "--wait", "node-2")

        step("run one reconciliation cycle")
        deadline = time.monotonic() + 90
        while True:
            report = run("exec", "-T", "reconciler", "python", "manage.py", "reconcile").strip()
            print(f"    report: {report.splitlines()[-1] if report else '{}'}")
            rep = api.detail(file_2["id"])["replication"]
            if rep["healthy"] == 3 or time.monotonic() > deadline:
                break
            time.sleep(3)
        expect(rep["healthy"] == 3 and not rep["under_replicated"], "second file re-replicated to 3/3")

        status, body = api.download(file_2["id"])
        expect(status == 200 and body == second, "second file downloads intact")
        print("\nDemo completed: all expectations met.")
        return 0
    except (DemoFailure, subprocess.CalledProcessError) as exc:
        detail = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else exc
        print(f"\nFAILED: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
