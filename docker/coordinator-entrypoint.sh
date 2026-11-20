#!/bin/sh
# Coordinator container entrypoint.
#  - Migrations run only when NODEMESH_AUTO_MIGRATE=true (isolated test stacks).
#    Against a shared database, run them deliberately: `manage.py migrate --plan` first.
#  - Nodes listed in NODEMESH_NODES are registered/updated (idempotent).
set -eu

if [ "${NODEMESH_AUTO_MIGRATE:-false}" = "true" ]; then
  python manage.py migrate --noinput
fi

if [ -n "${NODEMESH_NODES:-}" ]; then
  python manage.py register_node --from-env
fi

exec "$@"
