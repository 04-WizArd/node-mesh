#!/usr/bin/env python3
"""Create .env (Workflow A) or .env.cluster (Workflow B) from the example file.

Secrets are generated with the `secrets` module and written straight to the
file (mode 0600); they are never printed. Existing files are never overwritten.
Only the PostgreSQL password is left for you to fill in.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", action="store_true", help="write .env.cluster instead of .env")
    args = parser.parse_args()

    source = ROOT / (".env.cluster.example" if args.cluster else ".env.example")
    target = ROOT / (".env.cluster" if args.cluster else ".env")
    if target.exists():
        print(f"{target.name} already exists; not overwriting it.", file=sys.stderr)
        return 1

    text = source.read_text(encoding="utf-8")
    node_secrets = {n: secrets.token_hex(32) for n in range(1, 5)}
    key_id = "k1"

    def fill(match: re.Match) -> str:
        key = match.group(1)
        if key == "DJANGO_SECRET_KEY":
            value = secrets.token_urlsafe(50)
        elif key == "TEST_POSTGRES_PASSWORD":
            value = secrets.token_urlsafe(24)
        elif m := re.fullmatch(r"NODE_(\d)_HMAC_SECRET", key):
            value = node_secrets[int(m.group(1))]
        elif key == "NODEMESH_NODE_CREDENTIALS":
            value = ",".join(f"node-{n}:{key_id}:{s}" for n, s in node_secrets.items())
        else:
            return match.group(0)
        return f"{key}={value}"

    text = re.sub(r"^([A-Z0-9_]+)=<generated[^>]*>$", fill, text, flags=re.MULTILINE)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    todo = sorted(set(re.findall(r"^([A-Z0-9_]+)=<fill-in>$", text, flags=re.MULTILINE)))
    print(f"Wrote {target.name} with generated secrets (mode 0600).")
    if todo:
        print("Now fill in: " + ", ".join(todo))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
