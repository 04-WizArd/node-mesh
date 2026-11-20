#!/bin/sh
# Storage-node entrypoint: before serving, discard writes and deletions that a
# previous process left unfinished (nothing else can be in flight at this point).
set -eu
python manage.py cleanup_temp --max-age 0
exec "$@"
