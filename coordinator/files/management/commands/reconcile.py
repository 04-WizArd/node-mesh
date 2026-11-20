import json
import signal
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from files.reconcile import Reconciler


class Command(BaseCommand):
    help = "Probe nodes and converge replicas with metadata (one cycle, or --loop)."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Run forever.")
        parser.add_argument("--interval", type=float, default=30.0, help="Seconds between cycles.")

    def handle(self, *args, loop: bool, interval: float, **options):
        stopping = False

        def stop(signum, frame):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)

        while True:
            close_old_connections()
            report = Reconciler().run()
            if not loop:
                self.stdout.write(json.dumps(report, sort_keys=True))
                return
            deadline = time.monotonic() + interval
            while not stopping and time.monotonic() < deadline:
                time.sleep(0.5)
            if stopping:
                return
