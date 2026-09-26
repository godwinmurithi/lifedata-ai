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

For each dataset seen in real queries:
  * columns used as a real groupby/axis, cross-referenced against
    registry.yaml's declared columns -- flags any column actually queried
    that is NOT declared `dimension`/`temporal` there (a real usage the
    registry doesn't yet know about), and any dataset queried that has no
    registry.yaml entry at all.
  * metrics used, similarly cross-referenced against the dataset's declared
    `metrics:` list -- flags ad-hoc metrics used often enough that they look
    like a candidate for a real, governed metric.

Everything below is PRINTED as a reviewable suggestion. Nothing is written to
registry.yaml or to Superset -- per the plan's own rule for this phase
("proposes ... as reviewable suggestions, never silent writes").
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict

from django.core.management.base import BaseCommand

from lifedata_ai.semantics.loader import load_registry


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

        registry = load_registry()
        by_superset_id = {
            ds.superset_dataset_id: ds for ds in registry.datasets.values()
        }

        self.stdout.write(f"\n{len(seen_dataset_ids)} distinct datasets appear in real queries.\n")
        self.stdout.write("=" * 70 + "\n")

        for dataset_id in sorted(seen_dataset_ids):
            dataset = by_superset_id.get(dataset_id)
            col_counts = columns_by_dataset[dataset_id]
            metric_counts = metrics_by_dataset[dataset_id]

            if dataset is None:
                self.stdout.write(
                    f"\n[SUGGESTION] Dataset id={dataset_id} is queried "
                    f"{sum(col_counts.values()) + sum(metric_counts.values())} times in the "
                    "log but has NO registry.yaml entry. Columns actually used: "
                    f"{dict(col_counts.most_common(10))}. Candidate for cataloguing.\n"
                )
                continue

            declared_groupable = {
                c.name for c in dataset.columns if c.role in ("dimension", "temporal")
            }
            declared_metrics = {m.name for m in dataset.metrics}

            undeclared_columns = {
                col: n for col, n in col_counts.items() if col not in declared_groupable
            }
            undeclared_metrics = {
                m: n for m, n in metric_counts.items()
                if m.startswith("adhoc:") and n >= 3  # seen often enough to matter
            }

            if not undeclared_columns and not undeclared_metrics:
                continue

            self.stdout.write(f"\n[SUGGESTION] {dataset.name} (id={dataset_id}):\n")
            if undeclared_columns:
                self.stdout.write(
                    "  Columns queried but not declared dimension/temporal in "
                    f"registry.yaml: {undeclared_columns}\n"
                )
            if undeclared_metrics:
                self.stdout.write(
                    "  Ad-hoc metrics used repeatedly (candidates for a real, governed "
                    f"metric): {undeclared_metrics}\n"
                )

        self.stdout.write("\n" + "=" * 70)
        self.stdout.write(
            "\nNothing above was written anywhere. Review and apply by hand to "
            "registry.yaml.\n"
        )
