from django.core.management.base import BaseCommand

from objects.apps import get_nonce_store, get_store


class Command(BaseCommand):
    help = "Remove abandoned temporary writes, unfinished deletions and expired nonces."

    def add_arguments(self, parser):
        parser.add_argument(
            "--max-age",
            type=float,
            default=3600,
            help="Only remove entries older than this many seconds (use 0 before the server starts).",
        )

    def handle(self, *args, max_age: float, **options):
        removed = get_store().cleanup_stale(max_age)
        pruned = get_nonce_store().prune()
        self.stdout.write(f"removed {removed} stale entries, pruned {pruned} nonces")
