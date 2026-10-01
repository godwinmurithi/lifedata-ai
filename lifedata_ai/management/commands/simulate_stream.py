"""Phase 17: simulate a real-time stream (the plan's own permitted fallback).

    python manage.py simulate_stream                      # writes 1 event, exits
    python manage.py simulate_stream --count 5             # writes 5, exits
    python manage.py simulate_stream --loop --interval 120 # runs forever, one event per interval

No CDC/Debezium/NATS pipeline exists on this box (Phase 0 finding) and
standing one up was out of scope this session. The plan's own text names
this exact fallback: "simulate a stream by writing dummy events on a
timer." Event content is drawn from real district/facility names already
in `scd_screening_v2` (queried live, not hardcoded) so the simulated stream
looks like genuine ingestion traffic rather than obviously fake filler --
but the VALUES (counts, timestamps) are synthetic, generated here, not
read from anywhere real.
"""

from __future__ import annotations

import os
import random
import time
from datetime import UTC, datetime

from django.core.management.base import BaseCommand

from lifedata_ai.models import StreamEvent

_FALLBACK_DISTRICTS = ["Wakiso", "Iganga", "Kampala", "Gulu", "Mityana"]
_FALLBACK_FACILITIES = ["Buikwe HC IV", "Wakiso HC III", "Iganga Hospital"]


class Command(BaseCommand):
    help = "Simulate a real-time ingestion stream by writing dummy events on a timer."

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=1, help="Events to write, non-loop mode.")
        parser.add_argument("--loop", action="store_true", help="Run forever, one event per --interval.")
        parser.add_argument("--interval", type=int, default=120, help="Seconds between events in --loop mode.")

    def handle(self, *args, **options):
        districts, facilities = self._load_real_names()

        if options["loop"]:
            self.stdout.write(f"Simulating a stream every {options['interval']}s. Ctrl-C to stop.")
            try:
                while True:
                    self._write_one(districts, facilities)
                    time.sleep(options["interval"])
            except KeyboardInterrupt:
                self.stdout.write("\nStopped.")
            return

        for _ in range(options["count"]):
            self._write_one(districts, facilities)

    def _load_real_names(self) -> tuple[list[str], list[str]]:
        db_url = os.environ.get("LIFEDATA_AI_QUERY_LOG_DB_URL")
        if not db_url:
            return _FALLBACK_DISTRICTS, _FALLBACK_FACILITIES
        try:
            import psycopg

            conn = psycopg.connect(db_url)
            with conn.cursor() as cur:
                cur.execute("SELECT sql FROM tables WHERE id = 21")  # scd_screening_v2
                row = cur.fetchone()
                if not row or not row[0]:
                    return _FALLBACK_DISTRICTS, _FALLBACK_FACILITIES
                view_sql = row[0].replace("%", "%%")
                with conn.cursor() as cur2:
                    cur2.execute(f"WITH v AS ({view_sql}) SELECT DISTINCT district FROM v LIMIT 20")
                    districts = [r[0] for r in cur2.fetchall() if r[0]]
                with conn.cursor() as cur3:
                    cur3.execute(
                        f"WITH v AS ({view_sql}) SELECT DISTINCT testing_location FROM v LIMIT 20"
                    )
                    facilities = [r[0] for r in cur3.fetchall() if r[0]]
            conn.close()
            return districts or _FALLBACK_DISTRICTS, facilities or _FALLBACK_FACILITIES
        except Exception:  # noqa: BLE001 - a real DB isn't required for this to work
            return _FALLBACK_DISTRICTS, _FALLBACK_FACILITIES

    def _write_one(self, districts: list[str], facilities: list[str]) -> None:
        event_type = random.choice(list(StreamEvent.EventType))
        payload = {
            "district": random.choice(districts),
            "facility": random.choice(facilities),
            "simulated_at": datetime.now(UTC).isoformat(),
        }
        if event_type == StreamEvent.EventType.SCREENING_INGESTED:
            payload["result_category"] = random.choice(["Trait", "Disease", "Normal"])
        else:
            payload["referral_status"] = random.choice(["scheduled", "completed", "missed"])

        event = StreamEvent.objects.create(event_type=event_type, payload=payload)
        self.stdout.write(f"[{event.created_at:%H:%M:%S}] {event.event_type}: {payload}")
