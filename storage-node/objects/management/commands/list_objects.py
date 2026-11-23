"""Read-only inventory of this node's objects (used by `make cluster-storage`).

Reads only the metadata written when each object was stored: nothing is
created, modified or re-hashed. Use the internal ``objects/<id>/integrity``
endpoint (the reconciler's scrub) to re-verify the bytes themselves.
"""

from django.conf import settings
from django.core.management.base import BaseCommand

from objects.blobstore import BlobStore


class Command(BaseCommand):
    help = "List stored objects with their size and recorded SHA-256 (read-only)."

    def handle(self, *args, **options):
        # Not objects.apps.get_store(): that one creates missing directories.
        store = BlobStore(settings.NODE_DATA_DIR, max_object_bytes=settings.NODE_MAX_OBJECT_BYTES)
        unreadable: list[str] = []
        objects = list(store.iter_objects(unreadable=unreadable))
        total = sum(info.size for info in objects)

        self.stdout.write(f"node {settings.NODE_ID}, data directory {store.root}")
        self.stdout.write(f"objects: {len(objects)} ({total} bytes)")
        if objects:
            self.stdout.write(f"  {'OBJECT ID':<36}  {'SIZE':>12}  SHA-256")
            for info in objects:
                self.stdout.write(f"  {info.object_id!s:<36}  {info.size:>12}  {info.sha256}")
        if unreadable:
            self.stdout.write(f"unreadable entries: {len(unreadable)} (metadata missing or corrupt)")
            for key in unreadable:
                self.stdout.write(f"  {key}")
