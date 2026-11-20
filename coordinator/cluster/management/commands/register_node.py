import os
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from cluster.models import StorageNode, node_id_validator


def _validate(node_id: str, base_url: str) -> None:
    try:
        node_id_validator(node_id)
    except ValidationError as exc:
        raise CommandError(f"invalid node id {node_id!r}") from exc
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.path not in ("", "/"):
        raise CommandError(f"base URL for {node_id} must look like http://host:port (no path)")


class Command(BaseCommand):
    help = (
        "Register or update storage nodes. Either pass NODE_ID BASE_URL, or --from-env to read "
        "NODEMESH_NODES='node-1=http://node-1:8000,node-2=...'. Health state is never modified."
    )

    def add_arguments(self, parser):
        parser.add_argument("node_id", nargs="?")
        parser.add_argument("base_url", nargs="?")
        parser.add_argument("--state", choices=[s.value for s in StorageNode.AdminState])
        parser.add_argument("--from-env", action="store_true")

    def handle(self, *args, node_id, base_url, state, from_env, **options):
        if from_env:
            pairs = []
            for item in filter(None, (p.strip() for p in os.environ.get("NODEMESH_NODES", "").split(","))):
                nid, sep, url = item.partition("=")
                if not sep:
                    raise CommandError("NODEMESH_NODES entries must be node-id=url")
                pairs.append((nid.strip(), url.strip()))
            if not pairs:
                raise CommandError("NODEMESH_NODES is empty")
        elif node_id and base_url:
            pairs = [(node_id, base_url)]
        else:
            raise CommandError("pass NODE_ID BASE_URL or --from-env")

        for nid, url in pairs:
            _validate(nid, url)
            defaults = {"base_url": url.rstrip("/")}
            if state:
                defaults["admin_state"] = state
            node, created = StorageNode.objects.update_or_create(node_id=nid, defaults=defaults)
            verb = "registered" if created else "updated"
            self.stdout.write(f"{verb} {node.node_id} -> {node.base_url} ({node.admin_state})")
