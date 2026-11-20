#!/usr/bin/env python3
"""Read-only inventory of the coordinator database (no writes possible).

Uses POSTGRES_* from the environment or .env and opens a READ ONLY
transaction, so the server rejects any modification. Prints the server,
database and role, every table with its exact row count, and the Django
migrations recorded per app. Passwords are never printed.

    python scripts/db_inventory.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "src"))

import psycopg  # noqa: E402
from psycopg import sql  # noqa: E402

from nodemesh_common.env import env_int, env_str, load_env_file  # noqa: E402

NEW_TABLES = {"cluster_storagenode", "files_file", "files_replica"}
LEGACY_TABLES = {"node_storagenode", "node_file", "node_filechunk"}


def main() -> int:
    load_env_file(ROOT / ".env")
    conninfo = {
        "host": env_str("POSTGRES_HOST"),
        "port": env_int("POSTGRES_PORT", 5432),
        "dbname": env_str("POSTGRES_DB"),
        "user": env_str("POSTGRES_USER"),
        "password": env_str("POSTGRES_PASSWORD"),
        "connect_timeout": 5,
        "application_name": "nodemesh-inventory",
    }
    with psycopg.connect(**conninfo) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute("SELECT version(), current_database(), current_user, inet_server_port()")
            version, db, role, port = cur.fetchone()
            print(f"server   : {version.split(',')[0]}")
            print(f"database : {db}   role: {role}   server port: {port}")

            cur.execute("SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY datname")
            print("databases on this server:", ", ".join(r[0] for r in cur.fetchall()))

            cur.execute(
                """SELECT n.nspname, c.relname, pg_get_userbyid(c.relowner)
                   FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE c.relkind IN ('r', 'p')
                     AND n.nspname NOT IN ('pg_catalog', 'information_schema')
                     AND n.nspname NOT LIKE 'pg_toast%%'
                   ORDER BY 1, 2"""
            )
            tables = cur.fetchall()
            print(f"\n{len(tables)} table(s) in {db}:")
            names = set()
            for schema, table, owner in tables:
                query = sql.SQL("SELECT count(*) FROM {}.{}")
                cur.execute(query.format(sql.Identifier(schema), sql.Identifier(table)))
                count = cur.fetchone()[0]
                names.add(table)
                tag = " [new]" if table in NEW_TABLES else " [legacy]" if table in LEGACY_TABLES else ""
                print(f"  {schema}.{table:<32} rows={count:<8} owner={owner}{tag}")

            if "django_migrations" in names:
                cur.execute(
                    "SELECT app, count(*), max(applied) FROM django_migrations GROUP BY app ORDER BY app"
                )
                print("\napplied Django migrations:")
                for app, count, last in cur.fetchall():
                    print(f"  {app:<16} {count:>3} migration(s), last applied {last:%Y-%m-%d}")
            else:
                print("\nno django_migrations table: Django has never migrated this database")
        conn.rollback()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
