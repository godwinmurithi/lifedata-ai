"""Phase 17: wire the continuous analyst (and stream simulation) to a schedule.

    python manage.py run_scheduler

A single long-running process on two independent intervals, not Celery
beat + worker + Redis: this host runs with ~1.4GB free (GeoServer alone
holds 3GB -- see AI_NATIVE_STATUS.md's earlier OOM postmortem), and standing
up a full Celery stack for what is fundamentally two periodic jobs would
cost real memory for no real benefit over a plain loop. Run under the
`scheduler` service in deploy/sandbox/docker-compose.yml.

Each tick (every `--tick-seconds`, default 60) checks whether enough time
has passed since the last run of each job and, if so, runs it in-process
(not a subprocess -- avoids paying Python startup cost every tick). A
crash in one job is caught and logged; it does not kill the scheduler or
the other job's schedule.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

from django.core.management import call_command
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the continuous analyst and stream simulation on their own schedules, forever."

    def add_arguments(self, parser):
        parser.add_argument("--tick-seconds", type=int, default=60)
        parser.add_argument(
            "--analyst-interval", type=int, default=3600,
            help="Seconds between run_continuous_analyst invocations.",
        )
        parser.add_argument(
            "--stream-interval", type=int, default=180,
            help="Seconds between simulate_stream invocations.",
        )

    def handle(self, *args, **options):
        tick = options["tick_seconds"]
        analyst_interval = options["analyst_interval"]
        stream_interval = options["stream_interval"]
        last_analyst = 0.0
        last_stream = 0.0

        self.stdout.write(
            f"Scheduler started: continuous analyst every {analyst_interval}s, "
            f"stream simulation every {stream_interval}s, checking every {tick}s."
        )
        try:
            while True:
                now = time.monotonic()
                if now - last_analyst >= analyst_interval:
                    self._run_safely("run_continuous_analyst")
                    last_analyst = now
                if now - last_stream >= stream_interval:
                    self._run_safely("simulate_stream")
                    last_stream = now
                time.sleep(tick)
        except KeyboardInterrupt:
            self.stdout.write("\nScheduler stopped.")

    def _run_safely(self, command_name: str) -> None:
        timestamp = datetime.now(UTC).strftime("%H:%M:%S")
        self.stdout.write(f"[{timestamp}] running {command_name}")
        try:
            call_command(command_name)
        except Exception:  # noqa: BLE001 - one job's crash must not kill the scheduler
            logger.exception("Scheduled job %s failed", command_name)
            self.stderr.write(f"  {command_name} failed -- see logs, scheduler continues")
