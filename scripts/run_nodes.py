#!/usr/bin/env python3
"""Workflow A: run three (or more) storage nodes locally from ONE codebase.

Each node is a separate gunicorn process with its own NODE_ID, port, data
directory (var/nodes/<node-id>) and HMAC key, all read from .env.
Stop with Ctrl-C.

    python scripts/run_nodes.py              # node-1..node-3 on ports 8101..8103
    python scripts/run_nodes.py --count 4    # adds node-4 on 8104
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "src"))

from nodemesh_common.env import env_str, load_env_file  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--base-port", type=int, default=8101)
    args = parser.parse_args()

    load_env_file(ROOT / ".env")
    key_id = env_str("NODE_KEY_ID", "k1")
    procs: list[subprocess.Popen] = []
    for n in range(1, args.count + 1):
        node_id = f"node-{n}"
        port = args.base_port + n - 1
        data_dir = ROOT / "var" / "nodes" / node_id
        data_dir.mkdir(parents=True, exist_ok=True)
        env = {
            **os.environ,
            "NODE_ID": node_id,
            "NODE_DATA_DIR": str(data_dir),
            "NODE_HMAC_KEYS": f"{key_id}:{env_str(f'NODE_{n}_HMAC_SECRET')}",
            "NODE_MIN_FREE_BYTES": os.environ.get("NODE_MIN_FREE_BYTES", str(64 * 1024 * 1024)),
            "DJANGO_ALLOWED_HOSTS": "127.0.0.1,localhost",
        }
        subprocess.run(
            [sys.executable, "manage.py", "cleanup_temp", "--max-age", "0"],
            cwd=ROOT / "storage-node",
            env=env,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        cmd = [
            sys.executable,
            "-m",
            "gunicorn",
            "config.wsgi:application",
            "--bind",
            f"127.0.0.1:{port}",
            "--workers",
            "2",
            "--threads",
            "4",
            "--timeout",
            "300",
        ]
        procs.append(subprocess.Popen(cmd, cwd=ROOT / "storage-node", env=env))
        print(f"{node_id}: http://127.0.0.1:{port}  data={data_dir.relative_to(ROOT)}")

    def shutdown(*_):
        for proc in procs:
            proc.terminate()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    try:
        while all(p.poll() is None for p in procs):
            time.sleep(0.5)
    finally:
        shutdown()
        for proc in procs:
            proc.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
