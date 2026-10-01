"""Phase 11: mine Superset's own query log for real usage patterns.

    python manage.py mine_query_log

Reads `superset.logs` (`ChartDataRestApi.data` rows -- Superset's own record of
every chart-data query actually run, not a new logging mechanism this project
adds) directly against the staging metadata DB, since the orchestrator's own
Django DB user has no grant on the `superset` schema by design (checked: a
raw query from `django.db.connection` 403s with "permission denied for schema
superset" -- confirmed the isolation holds rather than assumed it). A
dedicated read-only connection string is required via
`LIFEDATA_AI_QUERY_LOG_DB_URL`, kept out of code per spec's secrets-from-env
rule.

For each dataset seen in real queries, this prints which columns and metrics
were actually used and how often.

This used to also cross-reference usage against the semantics registry's
declared columns/metrics, flagging real usage the registry did not yet know
about (and any queried dataset with no registry entry at all). The registry
has been removed, so that diffing is gone — this is now plain usage
reporting, nothing is written anywhere.
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Mine Superset's query log for real usage patterns, print reviewable suggestions."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit", type=int, default=5000,
            help="Max log rows to scan (most recent first).",
        )

    def handle(self, *args, **options):
        db_url = os.environ.get("LIFEDATA_AI_QUERY_LOG_DB_URL")
        if not db_url:
            self.stderr.write(
                "LIFEDATA_AI_QUERY_LOG_DB_URL is not set -- a read-only connection "
                "string to the Superset metadata DB (schema `superset`) is required. "
                "Not hardcoded here per the secrets-from-env rule."
            )
            return

        import psycopg

        conn = psycopg.connect(db_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT json FROM superset.logs "
                    "WHERE action = 'ChartDataRestApi.data' AND json IS NOT NULL "
                    "ORDER BY dttm DESC LIMIT %s",
                    (options["limit"],),
                )
                rows = cur.fetchall()
        finally:
            conn.close()

        self.stdout.write(f"Scanned {len(rows)} ChartDataRestApi.data log rows.\n")

        columns_by_dataset: dict[int, Counter] = defaultdict(Counter)
        metrics_by_dataset: dict[int, Counter] = defaultdict(Counter)
        seen_dataset_ids: set[int] = set()

        for (raw,) in rows:
            try:
                payload = raw if isinstance(raw, dict) else json.loads(raw)
            except (ValueError, TypeError):
                continue
            datasource = payload.get("datasource")
            dataset_id: int | None = None
            if isinstance(datasource, dict):
                dataset_id = datasource.get("id")
            elif isinstance(datasource, str) and "__" in datasource:
                # Older/alternate shape: "18__table" rather than {"id": 18, ...}
                head = datasource.split("__", 1)[0]
                if head.isdigit():
                    dataset_id = int(head)
            if not isinstance(dataset_id, int):
                continue
            seen_dataset_ids.add(dataset_id)
            for query in payload.get("queries") or [payload.get("form_data") or {}]:
                for col in query.get("columns") or query.get("groupby") or []:
                    if isinstance(col, str):
                        columns_by_dataset[dataset_id][col] += 1
                for metric in query.get("metrics") or []:
                    if isinstance(metric, str):
                        metrics_by_dataset[dataset_id][metric] += 1
                    elif isinstance(metric, dict):
                        label = metric.get("label") or metric.get("column", {}).get("column_name")
                        if label:
                            metrics_by_dataset[dataset_id][f"adhoc:{label}"] += 1

        self.stdout.write(f"\n{len(seen_dataset_ids)} distinct datasets appear in real queries.\n")
        self.stdout.write("=" * 70 + "\n")

        # This used to cross-reference each dataset against the semantics
        # registry's declared columns/metrics, flagging real usage the
        # registry did not yet know about. The registry has been removed, so
        # there is nothing left to diff against -- this now reports plain
        # usage (what was actually queried, how often), which is still real
        # operational signal on its own.
        for dataset_id in sorted(seen_dataset_ids):
            col_counts = columns_by_dataset[dataset_id]
            metric_counts = metrics_by_dataset[dataset_id]
            total = sum(col_counts.values()) + sum(metric_counts.values())

            self.stdout.write(f"\ndataset id={dataset_id} ({total} column/metric uses):\n")
            if col_counts:
                self.stdout.write(f"  columns used: {dict(col_counts.most_common(10))}\n")
            if metric_counts:
                self.stdout.write(f"  metrics used: {dict(metric_counts.most_common(10))}\n")

        self.stdout.write("\n" + "=" * 70 + "\n")
