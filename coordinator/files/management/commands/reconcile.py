import json
import logging
import signal
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from files.reconcile import Reconciler

logger = logging.getLogger(__name__)


def run_cycle(heartbeat: Path | None) -> dict | None:
    """Run one reconciliation cycle; touch ``heartbeat`` only if it succeeded.

    Returns the report, or ``None`` when the cycle failed (e.g. the database is
    unreachable). The heartbeat's age is what the container healthcheck reads,
    so a reconciler that keeps failing becomes *unhealthy* instead of silently
    crash-looping.
    """
    close_old_connections()
    try:
        report = Reconciler().run()
    except Exception:
        logger.exception("reconcile cycle failed")
        close_old_connections()
        return None
    if heartbeat is not None:
        heartbeat.parent.mkdir(parents=True, exist_ok=True)
        heartbeat.touch()
    return report


class Command(BaseCommand):
    help = "Probe nodes and converge replicas with metadata (one cycle, or --loop)."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Run forever.")
        parser.add_argument("--interval", type=float, default=30.0, help="Seconds between cycles.")
        parser.add_argument(
            "--heartbeat-file",
            type=Path,
            help="Touched after every successful cycle (read by the container healthcheck).",
        )

    def handle(self, *args, loop: bool, interval: float, heartbeat_file: Path | None, **options):
        if not loop:
            report = run_cycle(heartbeat_file)
            if report is None:
                raise CommandError("reconcile cycle failed (see log)")
            self.stdout.write(json.dumps(report, sort_keys=True))
            return

        stopping = False

        def stop(signum, frame):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)

        while not stopping:
            run_cycle(heartbeat_file)
            deadline = time.monotonic() + interval
            while not stopping and time.monotonic() < deadline:
                time.sleep(0.5)
