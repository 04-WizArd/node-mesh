#!/bin/sh
# Coordinator container entrypoint.
#
#  - Migrations run here ONLY when NODEMESH_AUTO_MIGRATE=true, which is meant
#    for throwaway test stacks. A shared database is migrated deliberately:
#    `make cluster-migrate-plan`, then `make cluster-migrate`.
#  - Otherwise the schema must already be current; if it is not (or the
#    database is unreachable) the container stops with an explicit message
#    instead of serving requests against an outdated schema.
#  - Nodes listed in NODEMESH_NODES are registered (idempotent; an existing
#    node is never silently re-pointed to a different URL).
set -eu

if [ "${NODEMESH_AUTO_MIGRATE:-false}" = "true" ]; then
  python manage.py migrate --noinput
elif ! python manage.py migrate --check >/dev/null 2>&1; then
  echo "nodemesh: the database schema is not up to date, or the database is unreachable." >&2
  echo "nodemesh: check POSTGRES_* in .env.cluster, then run: make cluster-migrate-plan && make cluster-migrate" >&2
  exit 1
fi

if [ -n "${NODEMESH_NODES:-}" ]; then
  python manage.py register_node --from-env
fi

exec "$@"
