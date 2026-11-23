#!/bin/sh
# Read-only inspection of the Docker storage nodes (`make cluster-storage`).
#
# For every storage-node service of the Compose project, shows its container
# state, the volume mounted at /data, and the objects it stores with their
# recorded SHA-256. Nothing is written: the node side runs the read-only
# `list_objects` management command.
#
# Usage: COMPOSE="docker compose --env-file .env.cluster -f compose.yaml -f ..." scripts/cluster_storage.sh
set -u

: "${COMPOSE:?set COMPOSE to the docker compose command of the cluster}"

# Storage nodes are the services named node-<n> (profile-disabled spares excluded).
nodes=$($COMPOSE config --services | grep -E '^node-[0-9]+$' | sort -t- -k2,2n)
if [ -z "$nodes" ]; then
  echo "No storage-node service found in the Compose configuration." >&2
  exit 1
fi

failures=0
for node in $nodes; do
  container=$($COMPOSE ps --all --quiet "$node" 2>/dev/null)
  if [ -z "$container" ]; then
    echo "== $node: no container (not created yet: run make cluster-up)"
    failures=$((failures + 1))
    echo
    continue
  fi

  state=$(docker inspect --format '{{.State.Status}}' "$container" 2>/dev/null || echo unknown)
  volume=$(docker inspect --format \
    '{{range .Mounts}}{{if eq .Destination "/data"}}{{if .Name}}{{.Name}}{{else}}{{.Source}}{{end}} -> {{.Destination}}{{end}}{{end}}' \
    "$container" 2>/dev/null)
  echo "== $node: $state, volume ${volume:-<no /data mount>}"

  if [ "$state" != "running" ]; then
    echo "   not running: objects cannot be listed (start it with: make cluster-up)"
    failures=$((failures + 1))
  elif output=$($COMPOSE exec -T "$node" python manage.py list_objects 2>&1); then
    printf '%s\n' "$output" | sed 's/^/   /'
  else
    printf '%s\n' "$output" | tail -n 5 | sed 's/^/   | /'
    echo "   inaccessible: the inventory command failed (see: make cluster-logs SERVICE=$node)"
    failures=$((failures + 1))
  fi
  echo
done

if [ "$failures" -gt 0 ]; then
  echo "$failures storage node(s) could not be inspected."
  exit 1
fi
