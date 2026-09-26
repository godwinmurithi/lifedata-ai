"""Phase 12 (Tier 3): continuous analyst -- anomaly detection + narrative.

    python manage.py run_continuous_analyst

Scheduled anomaly detection on a certified metric, decomposed by a key
dimension when an anomaly is flagged, written to the in-app
`AnalystFeedEntry` feed. Nothing is sent outward (email/Slack) -- per the
Phase 6 rule this project already holds, outward actions need explicit user
confirmation, and this command has no such confirmation step to attach to,
so it only ever writes the in-app record.

Runs its query directly against the staging metadata DB (same
`LIFEDATA_AI_QUERY_LOG_DB_URL` connection Phase 11's `mine_query_log` uses)
rather than through MCP's `execute_sql` -- tried that first and hit a real
gap: `scd_screening_v2` is a Superset *virtual* dataset (a SQL view defined
only inside Superset's own metadata, wrapping the real
`rhif_outreach_screening`/`newborn_screening_results` tables), and raw
`execute_sql` runs directly against Postgres with no knowledge of that
definition -- "relation scd_screening_v2 does not exist". Fetching the
dataset's own stored `sql` and wrapping this command's aggregation around
it as a CTE is what actually works, and does so without hardcoding a copy
of the view's logic that could drift from the real one.

This is genuinely NOT wired to run on a schedule yet -- there is no Celery
worker/beat for the *orchestrator* itself (the one built in Phase 5 belongs
to staging Superset, for its own thumbnail rendering, a different service).
Wiring this to actually run periodically needs either a new Celery beat
for lifedata-ai or a plain host cron entry calling this command -- flagged
as the concrete next step, not done here.
"""

from __future__ import annotations

import os
import statistics

from django.core.management.base import BaseCommand

from lifedata_ai.models import AnalystFeedEntry

#: |z| beyond this is flagged. 2.0 is a common, unremarkable default for a
#: prototype -- not tuned against this dataset's actual noise level, which
#: would need more history than the ~8 months available.
_Z_THRESHOLD = 2.0

_DATASET_ID = 21  # scd_screening_v2


class Command(BaseCommand):
    help = "Run anomaly detection on scd_screening_v2's monthly screening volume."

    def add_arguments(self, parser):
        parser.add_argument(
            "--threshold", type=float, default=_Z_THRESHOLD,
            help="Override the |z| flagging threshold (for testing the write path).",
        )

    def handle(self, *args, **options):
        threshold = options["threshold"]
        db_url = os.environ.get("LIFEDATA_AI_QUERY_LOG_DB_URL")
        if not db_url:
            self.stderr.write("LIFEDATA_AI_QUERY_LOG_DB_URL is not set.")
            return

        import psycopg

        conn = psycopg.connect(db_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT sql FROM superset.tables WHERE id = %s", (_DATASET_ID,)
                )
                row = cur.fetchone()
                if not row or not row[0]:
                    self.stderr.write(f"Dataset {_DATASET_ID} has no stored virtual SQL.")
                    return
                # The view's own SQL contains literal '%' (ILIKE patterns like
                # 'Trait Not Seen%') which psycopg's %s-style parameter
                # binding otherwise misreads as a placeholder the moment it's
                # embedded in the same f-string as a real %s -- doubled here
                # so it survives as a literal percent, not escaped in the
                # query text itself.
                view_sql = row[0].replace("%", "%%")

                with conn.cursor() as cur:
                    cur.execute(
                        f"WITH v AS ({view_sql}) "
                        "SELECT collection_month, COUNT(*) AS n FROM v "
                        "GROUP BY collection_month ORDER BY collection_month"
                    )
                    rows = [{"collection_month": r[0], "n": r[1]} for r in cur.fetchall()]

                if len(rows) < 3:
                    self.stdout.write("Not enough history to detect an anomaly.")
                    return

                *history, latest = rows
                counts = [r["n"] for r in history]
                mean = statistics.mean(counts)
                stdev = statistics.pstdev(counts) or 1.0
                z = (latest["n"] - mean) / stdev

                self.stdout.write(
                    f"Latest period {latest['collection_month']}: {latest['n']} screenings "
                    f"(baseline mean={mean:.1f}, stdev={stdev:.1f}, z={z:.2f})"
                )

                if abs(z) < threshold:
                    self.stdout.write("No anomaly flagged.")
                    return

                direction = "spike" if z > 0 else "drop"
                self.stdout.write(
                    f"ANOMALY: {direction} flagged (|z|={abs(z):.2f} >= {threshold})"
                )

                with conn.cursor() as cur:
                    cur.execute(
                        f"WITH v AS ({view_sql}) "
                        "SELECT district, COUNT(*) AS n FROM v "
                        "WHERE collection_month = %s "
                        "GROUP BY district ORDER BY n DESC LIMIT 10",
                        (latest["collection_month"],),
                    )
                    decomp_rows = cur.fetchall()
        finally:
            conn.close()

        decomposition = {r[0]: r[1] for r in decomp_rows if r[0]}
        top = ", ".join(f"{k} ({v})" for k, v in list(decomposition.items())[:5])
        narrative = (
            f"Sickle cell screening volume {direction}ped to {latest['n']} in "
            f"{latest['collection_month']}, versus a baseline average of {mean:.0f} "
            f"across the prior {len(counts)} months (z={z:.1f}). "
            + (f"By district, the largest contributors this period: {top}." if top else
               "No district breakdown available for this period.")
        )
        self.stdout.write(narrative)

        AnalystFeedEntry.objects.create(
            metric_name="scd_screening_count",
            dataset_name="scd_screening_v2",
            period_label=str(latest["collection_month"]),
            observed_value=float(latest["n"]),
            expected_value=float(mean),
            z_score=float(z),
            narrative=narrative,
            decomposition=decomposition,
        )
        self.stdout.write(self.style.SUCCESS("Written to the in-app feed (AnalystFeedEntry)."))
