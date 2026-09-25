"""Register the reporting views as Superset datasets, and curate their metadata.

This is a SETUP command, not part of the runtime path. It authenticates as the
Superset admin to create the analytics database connection, the datasets, and —
with `--sync-metadata`, which runs by default — the curated column labels and
saved metrics that `semantics/registry.yaml` declares.

The runtime identity model is unaffected: the copilot never uses these
credentials. It only ever acts with the end user's own forwarded session.

    python manage.py register_superset_datasets
    python manage.py register_superset_datasets --no-sync-metadata   # ids only

# Why the metadata projection exists

`registry.yaml` is the single source for what the model is told, what the
validator permits, and what Superset renders. The first two are read directly.
The third has to be *pushed*, because Superset keeps its own copy of column and
metric metadata in its database and nothing reads a YAML file at query time.

The projection is what makes three governed indicators chartable at all.
`confirmed_diagnoses` is a `COUNT(*) FILTER`, and `malaria_positivity` and
`lab_completion_rate` are ratios; none can be expressed as `{aggregate, column}`,
and widening the schema so they could would dismantle the no-SQL rule. As saved
metrics they are just names — `"metrics": ["malaria_positivity"]` — which pass
through `_to_superset_metric` untouched and clear validation as identifiers.

# The trap in PUT /api/v1/dataset/{id}

`PUT` REPLACES the `columns` collection rather than merging into it, and each
column is identified by an integer `id` that Superset assigned. Sending a column
list built from `registry.yaml` alone therefore DELETES every column and
recreates them with new ids, which orphans every chart that referenced one. So
the command reads the dataset first and merges label and description onto the
entries it already has, preserving `id` on each.

# Runbook

Re-run this after any "Sync columns from source" in the Superset UI. That button
rewrites the column collection from the database schema and wipes every curated
`verbose_name` and `description`. Nothing in CI can catch it, because it happens
in a browser against a running instance.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
from django.core.management.base import BaseCommand, CommandError

from lifedata_ai.semantics.loader import Dataset, load_registry

#: The views the semantics registry maps onto (spec §8).
REPORTING_VIEWS = [
    "rpt_facilities",
    "rpt_encounters",
    "rpt_lab_orders",
    "rpt_patient_geography",
    "rpt_diabetes_screening",
]

DATABASE_NAME = "LifeHealth (synthetic)"
#: Superset connects with the read-only role; it cannot see `public.*` at all.
SQLALCHEMY_URI = "postgresql+psycopg2://superset_ro:superset-ro-dev-password@postgres:5432/lifedata"

#: Column fields the projection is allowed to touch. Everything else on a column
#: — its type, whether it is a dttm, its expression — belongs to Superset's own
#: introspection and must survive the round trip untouched.
_CURATED_COLUMN_FIELDS = ("verbose_name", "description")


class Command(BaseCommand):
    help = (
        "Create the Superset database connection and datasets for the reporting views, "
        "and project registry.yaml's labels and saved metrics onto them."
    )

    def add_arguments(self, parser):
        parser.add_argument("--superset-url", default=os.environ.get(
            "LIFEDATA_AI_SUPERSET_INTERNAL_URL", "http://superset:8088"))
        parser.add_argument("--username", default=os.environ.get("SUPERSET_ADMIN_USERNAME", "admin"))
        parser.add_argument("--password", default=os.environ.get("SUPERSET_ADMIN_PASSWORD", "admin"))
        parser.add_argument(
            "--sync-metadata",
            action=argparse_bool_action(),
            default=True,
            help=(
                "Project registry.yaml's column labels, descriptions, time column and "
                "saved metrics onto each dataset. On by default; --no-sync-metadata "
                "registers the datasets and stops."
            ),
        )
        parser.add_argument(
            "--adopt-existing",
            action="store_true",
            help=(
                "Do not create anything. Project registry.yaml onto the datasets it "
                "already names in `superset_dataset_id`. This is the mode for a "
                "deployment layered onto a Superset that was already in use."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what --adopt-existing would change, and write nothing.",
        )

    def handle(self, *args, **options):
        base = options["superset_url"].rstrip("/")
        registry = load_registry()

        with httpx.Client(timeout=httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)) as c:
            token = self._login(c, base, options["username"], options["password"])
            headers = {"Authorization": f"Bearer {token}"}
            csrf, cookies = self._csrf(c, base, headers)
            headers["X-CSRFToken"] = csrf

            if options["adopt_existing"]:
                self._adopt_existing(c, base, headers, cookies, registry, options["dry_run"])
                return

            database_id = self._ensure_database(c, base, headers, cookies)
            self.stdout.write(f"database id: {database_id}")

            results = {}
            for view in REPORTING_VIEWS:
                results[view] = self._ensure_dataset(c, base, headers, cookies, database_id, view)

            if options["sync_metadata"]:
                self.stdout.write("")
                self.stdout.write("Projecting registry.yaml onto Superset:")
                for view, dataset_id in results.items():
                    declared = registry.dataset(view)
                    if declared is None:
                        self.stdout.write(
                            self.style.WARNING(f"  {view}: not in registry.yaml; skipped")
                        )
                        continue
                    self._sync_metadata(c, base, headers, cookies, dataset_id, declared)

        self.stdout.write(self.style.SUCCESS("\nDataset IDs — put these in semantics/registry.yaml:"))
        for view, dataset_id in results.items():
            self.stdout.write(f"  {view:<26} superset_dataset_id: {dataset_id}")

        if options["sync_metadata"]:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    "RUNBOOK: re-run this after any \"Sync columns from source\" in the "
                    "Superset UI. That button rebuilds the column collection from the "
                    "database schema and wipes every curated label."
                )
            )

    # -- steps ------------------------------------------------------------

    def _login(self, c: httpx.Client, base: str, username: str, password: str) -> str:
        r = c.post(
            f"{base}/api/v1/security/login",
            json={"username": username, "password": password,
                  "provider": "db", "refresh": True},
        )
        if r.status_code != 200:
            raise CommandError(f"Superset login failed ({r.status_code}): {r.text[:300]}")
        return r.json()["access_token"]

    def _csrf(self, c: httpx.Client, base: str, headers: dict) -> tuple[str, dict]:
        r = c.get(f"{base}/api/v1/security/csrf_token/", headers=headers)
        if r.status_code != 200:
            raise CommandError(f"Could not obtain CSRF token ({r.status_code}): {r.text[:200]}")
        return r.json()["result"], dict(r.cookies)

    def _ensure_database(self, c, base, headers, cookies) -> int:
        existing = c.get(
            f"{base}/api/v1/database/",
            headers=headers,
            params={"q": json.dumps({"filters": [
                {"col": "database_name", "opr": "eq", "value": DATABASE_NAME}]})},
        )
        if existing.status_code == 200 and existing.json().get("count"):
            found = existing.json()["result"][0]["id"]
            self.stdout.write(f"database '{DATABASE_NAME}' already registered (id={found})")
            return found

        r = c.post(
            f"{base}/api/v1/database/",
            headers=headers,
            cookies=cookies,
            json={
                "database_name": DATABASE_NAME,
                "sqlalchemy_uri": SQLALCHEMY_URI,
                "expose_in_sqllab": False,
                "allow_ctas": False,
                "allow_cvas": False,
                "allow_dml": False,
            },
        )
        if r.status_code not in (200, 201):
            raise CommandError(f"Creating database failed ({r.status_code}): {r.text[:400]}")
        return r.json()["id"]

    def _ensure_dataset(self, c, base, headers, cookies, database_id: int, view: str) -> int:
        existing = c.get(
            f"{base}/api/v1/dataset/",
            headers=headers,
            params={"q": json.dumps({"filters": [
                {"col": "table_name", "opr": "eq", "value": view}]})},
        )
        if existing.status_code == 200 and existing.json().get("count"):
            found = existing.json()["result"][0]["id"]
            self.stdout.write(f"  dataset {view} exists (id={found})")
            return found

        r = c.post(
            f"{base}/api/v1/dataset/",
            headers=headers,
            cookies=cookies,
            json={"database": database_id, "schema": "reporting", "table_name": view},
        )
        if r.status_code not in (200, 201):
            raise CommandError(f"Creating dataset {view} failed ({r.status_code}): {r.text[:400]}")
        dataset_id = r.json()["id"]
        self.stdout.write(f"  dataset {view} created (id={dataset_id})")
        return dataset_id

    # -- adopting a Superset that was already in use -----------------------

    def _adopt_existing(self, c, base, headers, cookies, registry, dry_run: bool) -> None:
        """Project the registry onto datasets that already exist.

        The default path is built for a Superset this project stood up: it
        creates a database connection and a dataset per entry in
        `REPORTING_VIEWS`, then syncs. Pointed at an existing deployment that is
        exactly wrong — it would add a connection to a host that is not there
        and create datasets nobody asked for, beside the real ones.

        What that deployment needs is only the last step, against ids the
        registry already carries. Without it the registry's saved metrics do not
        exist in Superset at all, and the copilot's first chart fails with
        `Metric 'x' does not exist` — observed exactly that way on the CTI
        sandbox. The compile check catches it before anything is created and the
        model recovers by falling back to a simple aggregation, so the symptom is
        quiet: charts still appear, just never using a governed metric.

        `--dry-run` first. This writes `verbose_name` and `description` onto
        columns of datasets somebody else curated, and seeing that diff before it
        lands is worth the extra command.
        """
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — nothing will be written.\n"))

        declared_datasets = [d for d in registry.datasets.values() if d.superset_dataset_id]
        if not declared_datasets:
            raise CommandError(
                "No dataset in registry.yaml carries a `superset_dataset_id`, so there is "
                "nothing to adopt. Add the live ids first."
            )

        for declared in declared_datasets:
            dataset_id = declared.superset_dataset_id
            try:
                current = self._get_dataset(c, base, headers, dataset_id)
            except CommandError as exc:
                # A registry pointing at an id that does not exist is a
                # configuration error worth stopping on, not skipping past: every
                # chart against it would fail later, further from the cause.
                raise CommandError(
                    f"{declared.name}: dataset id {dataset_id} could not be read from "
                    f"Superset. Check `superset_dataset_id` in registry.yaml. ({exc})"
                ) from exc

            table_name = current.get("table_name")
            if table_name and table_name != declared.name:
                # Not fatal — a registry name is ours and need not match — but it
                # is the first thing to check when ids have shifted under a
                # metadata restore.
                self.stdout.write(
                    self.style.WARNING(
                        f"  note: id {dataset_id} is '{table_name}' in Superset, "
                        f"declared as '{declared.name}' in registry.yaml"
                    )
                )

            if dry_run:
                self._report_diff(current, declared, dataset_id)
                continue
            self._sync_metadata(c, base, headers, cookies, dataset_id, declared)

        if dry_run:
            self.stdout.write(
                self.style.WARNING("\nDRY RUN — re-run without --dry-run to apply.")
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(f"\nAdopted {len(declared_datasets)} existing dataset(s).")
            )
            self.stdout.write(
                self.style.WARNING(
                    "RUNBOOK: re-run this after any \"Sync columns from source\" in the "
                    "Superset UI. That button rebuilds the column collection from the "
                    "database schema and wipes every curated label."
                )
            )

    def _report_diff(self, current: dict[str, Any], declared: Dataset, dataset_id: int) -> None:
        """Print what `_sync_metadata` would change, and write nothing."""
        existing_metrics = {m.get("metric_name") for m in current.get("metrics") or []}
        new_metrics = [m.name for m in declared.metrics if m.name not in existing_metrics]

        relabelled: list[str] = []
        declared_columns = {c.name: c for c in declared.columns}
        for column in current.get("columns") or []:
            wanted = declared_columns.get(column.get("column_name"))
            if wanted is None:
                continue
            label = getattr(wanted, "display_label", None) or ""
            if label and (column.get("verbose_name") or "") != label:
                relabelled.append(
                    f"{column['column_name']}: "
                    f"{column.get('verbose_name') or '(none)'!r} -> {label!r}"
                )

        self.stdout.write(f"\n  {declared.name} (dataset {dataset_id})")
        self.stdout.write(
            f"    metrics to add ({len(new_metrics)}): {', '.join(new_metrics) or 'none'}"
        )
        self.stdout.write(f"    column labels to change ({len(relabelled)}):")
        for line in relabelled[:12]:
            self.stdout.write(f"      {line}")
        if len(relabelled) > 12:
            self.stdout.write(f"      ... and {len(relabelled) - 12} more")

        wanted_description = declared.one_row_is or declared.notes
        if (current.get("description") or "") != (wanted_description or ""):
            self.stdout.write(
                f"    description -> {str(wanted_description or '')[:100]!r}"
            )

    # -- the projection ---------------------------------------------------

    def _sync_metadata(
        self, c, base, headers, cookies, dataset_id: int, declared: Dataset
    ) -> None:
        """Push labels, descriptions, the time column and saved metrics.

        Idempotent and re-runnable: it reads first, merges, and writes back, so
        running it twice produces the same dataset and running it after a manual
        Superset edit restores the curated values without discarding Superset's
        own introspected ones.
        """
        current = self._get_dataset(c, base, headers, dataset_id)

        columns, unknown, unlabelled = merge_columns(current.get("columns") or [], declared)
        metrics = merge_metrics(current.get("metrics") or [], declared)

        body: dict[str, Any] = {
            "columns": columns,
            "metrics": metrics,
            "description": declared.one_row_is or declared.notes,
        }
        if declared.time_column:
            body["main_dttm_col"] = declared.time_column

        r = c.put(
            f"{base}/api/v1/dataset/{dataset_id}",
            headers=headers,
            cookies=cookies,
            # Superset validates the whole payload including columns it did not
            # ask about; overriding lets a curated label reach a column whose
            # other fields Superset would otherwise treat as a change.
            params={"override_columns": "true"},
            json=body,
        )
        if r.status_code not in (200, 201):
            raise CommandError(
                f"Syncing metadata for dataset {dataset_id} ({declared.name}) failed "
                f"({r.status_code}): {r.text[:500]}"
            )

        self.stdout.write(
            f"  {declared.name:<26} {len(columns)} columns, {len(metrics)} metrics"
        )
        # These two are the ones that bite silently: a column in the view that
        # nobody declared cannot be charted (the validator's allowlist refuses
        # it), and a declared column that no longer exists in the view means the
        # catalogue is offering the model something that will fail at query time.
        if unlabelled:
            self.stdout.write(
                self.style.WARNING(
                    f"    {len(unlabelled)} column(s) in the view are not in registry.yaml "
                    f"and so cannot be charted: {', '.join(sorted(unlabelled))}"
                )
            )
        if unknown:
            self.stdout.write(
                self.style.ERROR(
                    f"    registry.yaml declares {len(unknown)} column(s) the view does not "
                    f"have: {', '.join(sorted(unknown))}. The catalogue is offering the "
                    "model something that will fail at query time."
                )
            )

    def _get_dataset(self, c, base, headers, dataset_id: int) -> dict[str, Any]:
        r = c.get(f"{base}/api/v1/dataset/{dataset_id}", headers=headers)
        if r.status_code != 200:
            raise CommandError(
                f"Could not read dataset {dataset_id} ({r.status_code}): {r.text[:300]}"
            )
        return r.json().get("result") or {}


# --- The merges, as pure functions ---------------------------------------
#
# Separated from the HTTP so they can be tested without a Superset. Both are
# where the damage would be done if they were wrong, and the damage is silent.


def merge_columns(
    existing: list[dict[str, Any]], declared: Dataset
) -> tuple[list[dict[str, Any]], set[str], set[str]]:
    """Lay registry labels over Superset's own column records.

    Returns `(columns, declared_but_absent, present_but_undeclared)`.

    **Every existing entry keeps its `id`.** `PUT` replaces the collection, so a
    column list built from the registry alone would delete all of Superset's
    columns and recreate them with fresh ids — orphaning every chart that
    referenced one, silently, with no error anywhere. This function never
    invents a column and never drops one; it only sets `verbose_name` and
    `description` on what is already there.
    """
    columns: list[dict[str, Any]] = []
    seen: set[str] = set()

    for column in existing:
        name = column.get("column_name")
        if not name:
            continue
        seen.add(name)
        entry = dict(column)
        declared_column = declared.column(name)
        if declared_column is not None:
            if declared_column.label:
                entry["verbose_name"] = declared_column.label
            if declared_column.note:
                entry["description"] = declared_column.note
        # Superset rejects some read-only fields on the way back in.
        for read_only in ("changed_on", "created_on", "type_generic", "uuid"):
            entry.pop(read_only, None)
        columns.append(entry)

    declared_names = {c.name for c in declared.columns}
    return columns, declared_names - seen, seen - declared_names


def merge_metrics(existing: list[dict[str, Any]], declared: Dataset) -> list[dict[str, Any]]:
    """Add or update the registry's saved metrics, keeping everything else.

    Superset creates a `count` metric on every dataset and a user may have added
    their own; neither is ours to remove. A metric the registry declares is
    matched by name and updated in place — keeping its `id`, for the same reason
    columns do — and one it does not know about is passed through untouched.
    """
    by_name = {m.get("metric_name"): dict(m) for m in existing if m.get("metric_name")}

    for metric in declared.metrics:
        entry = by_name.get(metric.name, {})
        entry.update(
            {
                "metric_name": metric.name,
                "expression": metric.expression,
                "verbose_name": metric.display_label,
                "d3format": metric.d3format,
                "description": _metric_description(metric),
            }
        )
        by_name[metric.name] = entry

    metrics: list[dict[str, Any]] = []
    for entry in by_name.values():
        cleaned = dict(entry)
        for read_only in ("changed_on", "created_on", "uuid"):
            cleaned.pop(read_only, None)
        metrics.append(cleaned)
    return metrics


def _metric_description(metric: Any) -> str:
    """What a Superset user sees when they hover the metric.

    Carries the governed indicator's name where there is one, so someone reading
    the dataset in Superset can find the definition the copilot quotes. That
    back-link is the point of `indicator:` in registry.yaml — it keeps the
    definition explained and the number drawn on screen in step.
    """
    if metric.indicator:
        return (
            f"LifeData governed indicator “{metric.indicator}”. Definition and caveats are "
            "in lifedata_ai/semantics/registry.yaml; do not edit this metric here, edit "
            "the registry and re-run register_superset_datasets."
        )
    return (
        "Curated by LifeData. Edit lifedata_ai/semantics/registry.yaml and re-run "
        "register_superset_datasets rather than editing it here."
    )


def argparse_bool_action():
    """`--sync-metadata` / `--no-sync-metadata`, on whichever Python is running.

    `argparse.BooleanOptionalAction` has been available since 3.9 and this
    project requires 3.12, so it is used directly; the indirection exists only to
    keep the argument declaration above readable.
    """
    import argparse

    return argparse.BooleanOptionalAction
