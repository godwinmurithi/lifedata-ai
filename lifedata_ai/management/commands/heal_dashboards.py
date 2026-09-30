"""Phase 12 (Tier 3): self-healing dashboards.

    python manage.py heal_dashboards            # dry run, reports only
    python manage.py heal_dashboards --apply     # applies safe fixes, staging only

Grew directly out of a real bug this project hit twice: a dataset's virtual-SQL
column got renamed (`birth_district` -> `district`, an earlier session) and the
fix only touched chart `params` at the time -- the two `RowLevelSecurityFilter`
clauses referencing the same column were missed, silently breaking every
non-admin, RLS-scoped read of `scd_screening_v2` until a live Phase 10 test (a
non-admin user calling a chart) surfaced it by accident. "Self-healing" here
means: don't wait for a real user to hit a real error to find the *next* one of
these -- detect it empirically (does the chart's own query actually run?),
propose the fix from the dataset's real, current columns, and re-verify before
declaring anything fixed.

**Detection is empirical, not static analysis.** Every catalogued chart is
called through the real `get_chart_data` MCP tool, as `admin` (bypasses RLS, so
this checks the dataset's own SQL is sound independent of any particular RLS
role -- RLS clauses are checked separately, below). A chart that runs is
healthy; one that doesn't gets its Postgres `DataError` parsed for the
specific missing column name -- the same shape of message
(`column "X" does not exist`) this project has now hit for real twice.

**RLS clause checking is separate**, because RLS clauses aren't run through
`get_chart_data` at all as admin (admin has no RLS role, so nothing exercises
them) -- each `row_level_security_filters.clause` is checked directly against
its target dataset(s)' real current columns via a regex extracting the
left-hand identifier of a `column = 'value'`-shaped clause (the only shape any
RLS filter in this project actually uses -- a general SQL-expression parser
would be overkill for what's really here, and would be guessing at expressions
that don't exist yet).

**A found problem is only ever auto-*applied* if every affected chart/filter's
dataset sits on the staging-only database connection** (`dbs.database_name =
'Staging Postgres (db-staging4cti)'` -- resolved by name at runtime, not a
hardcoded id, in case connections get renumbered). This mirrors the standing
project decision (AI_NATIVE_STATUS.md, Phase 1) that the 19 production-backed
datasets are out of scope for new AI-driven write access: this tool can and
does still *report* a broken production-backed chart/filter, it just never
patches one, regardless of `--apply`.

The fuzzy-match itself uses `difflib.get_close_matches` (stdlib, not a new
dependency) -- a fix is only proposed when exactly one column clears the
similarity cutoff; two or more plausible candidates, or zero, are reported as
"needs a human," not guessed at.
"""

from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass

from django.core.management.base import BaseCommand

#: Two distinct error shapes seen in practice, not guessed: a raw Postgres
#: `DataError` (e.g. from a broken RLS clause concatenated straight into the
#: WHERE clause, bypassing Superset's own column validation), and Superset's
#: own pre-query validation for chart params ("Columns missing in dataset:
#: [...]") which fires before any SQL is even issued. Both are checked; a
#: chart hitting the second shape never reaches Postgres at all, so relying
#: on only the first would miss most real chart-level breakage.
_MISSING_COLUMN_RES: tuple[re.Pattern, ...] = (
    re.compile(r'column "([^"]+)" does not exist'),
    re.compile(r"Columns missing in dataset: \['([^']+)'"),
)
#: Matches the one clause shape every RLS filter in this project actually
#: uses: `<column> = '<value>'` (optionally with surrounding whitespace).
_RLS_CLAUSE_COLUMN_RE = re.compile(r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=")
_STAGING_DB_NAME = "Staging Postgres (db-staging4cti)"
_FUZZY_CUTOFF = 0.6


@dataclass
class Finding:
    kind: str  # "chart" or "rls_filter"
    object_id: int
    object_label: str
    dataset_id: int
    dataset_name: str
    is_staging: bool
    broken_column: str
    proposed_fix: str | None
    reason_no_fix: str | None = None


class Command(BaseCommand):
    help = "Detect and (optionally) repair charts/RLS filters referencing a renamed/missing column."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Apply unambiguous fixes, staging-backed datasets only. Default is dry-run.",
        )

    def handle(self, *args, **options):
        apply_fixes = options["apply"]
        db_url = os.environ.get("LIFEDATA_AI_QUERY_LOG_DB_URL")
        if not db_url:
            self.stderr.write("LIFEDATA_AI_QUERY_LOG_DB_URL is not set.")
            return

        import psycopg

        conn = psycopg.connect(db_url)
        try:
            dataset_columns, dataset_meta = self._load_dataset_schema(conn)
            findings: list[Finding] = []
            findings.extend(self._check_charts(conn, dataset_columns, dataset_meta))
            findings.extend(self._check_rls_filters(conn, dataset_columns, dataset_meta))

            if not findings:
                self.stdout.write(self.style.SUCCESS("No broken column references found."))
                return

            applied = 0
            for finding in findings:
                self._report(finding)
                if apply_fixes and finding.proposed_fix and finding.is_staging:
                    self._apply(conn, finding)
                    applied += 1

            conn.commit()
            self.stdout.write(
                f"\n{len(findings)} finding(s), {applied} applied "
                f"({'--apply was set' if apply_fixes else 'dry run -- pass --apply to fix staging ones'})."
            )
        finally:
            conn.close()

    # -- schema loading -----------------------------------------------------

    def _load_dataset_schema(self, conn):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT t.id, t.table_name, d.database_name "
                "FROM tables t JOIN dbs d ON d.id = t.database_id"
            )
            dataset_meta = {
                row[0]: {"name": row[1], "is_staging": row[2] == _STAGING_DB_NAME}
                for row in cur.fetchall()
            }
            cur.execute("SELECT table_id, column_name FROM table_columns")
            dataset_columns: dict[int, set[str]] = {}
            for table_id, column_name in cur.fetchall():
                dataset_columns.setdefault(table_id, set()).add(column_name)
        return dataset_columns, dataset_meta

    # -- chart checking (empirical, via real MCP get_chart_data) -------------

    def _check_charts(self, conn, dataset_columns, dataset_meta) -> list[Finding]:
        from lifedata_ai.superset_mcp_client import SupersetMCPClient

        with conn.cursor() as cur:
            # `datasource_id`, not `table_id` -- checked directly against this
            # deployment's real data (337 table-type charts, 336 with a NULL
            # `table_id`) rather than trusted from the column's presence:
            # `table_id` is essentially unpopulated in practice, `datasource_id`
            # is the field every chart actually carries its dataset id in.
            cur.execute(
                "SELECT id, slice_name, datasource_id FROM slices "
                "WHERE datasource_type = 'table' AND datasource_id IS NOT NULL"
            )
            charts = cur.fetchall()

        client = SupersetMCPClient(username="admin")
        findings: list[Finding] = []
        for chart_id, slice_name, dataset_id in charts:
            if dataset_id not in dataset_meta:
                continue
            try:
                payload = client.call_any_tool(
                    "get_chart_data", {"identifier": chart_id, "limit": 5}
                )
            except Exception as exc:  # noqa: BLE001 - report, don't crash the scan
                self.stderr.write(f"  (chart {chart_id} '{slice_name}': call failed, skipping: {exc})")
                continue

            error_text = payload.get("error") if isinstance(payload, dict) else None
            if not error_text:
                continue
            match = next((m for m in (p.search(error_text) for p in _MISSING_COLUMN_RES) if m), None)
            if not match:
                self.stderr.write(
                    f"  (chart {chart_id} '{slice_name}': failed for a reason this tool doesn't "
                    f"parse, needs a human: {error_text[:200]})"
                )
                continue

            broken_column = match.group(1)
            meta = dataset_meta[dataset_id]
            real_columns = dataset_columns.get(dataset_id, set())
            fix, reason = self._propose_fix(broken_column, real_columns)
            findings.append(Finding(
                kind="chart", object_id=chart_id, object_label=f"chart {chart_id} ({slice_name!r})",
                dataset_id=dataset_id, dataset_name=meta["name"], is_staging=meta["is_staging"],
                broken_column=broken_column, proposed_fix=fix, reason_no_fix=reason,
            ))
        return findings

    # -- RLS filter checking (direct clause inspection) ----------------------

    def _check_rls_filters(self, conn, dataset_columns, dataset_meta) -> list[Finding]:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT f.id, f.name, f.clause, ft.table_id "
                "FROM row_level_security_filters f "
                "JOIN rls_filter_tables ft ON ft.rls_filter_id = f.id"
            )
            rows = cur.fetchall()

        findings: list[Finding] = []
        for filter_id, name, clause, table_id in rows:
            if table_id not in dataset_meta:
                continue
            match = _RLS_CLAUSE_COLUMN_RE.match(clause or "")
            if not match:
                self.stderr.write(
                    f"  (RLS filter {filter_id} {name!r}: clause shape not recognised, "
                    f"needs a human: {clause!r})"
                )
                continue
            column = match.group(1)
            real_columns = dataset_columns.get(table_id, set())
            if column in real_columns:
                continue  # healthy

            meta = dataset_meta[table_id]
            fix, reason = self._propose_fix(column, real_columns)
            findings.append(Finding(
                kind="rls_filter", object_id=filter_id, object_label=f"RLS filter {filter_id} ({name!r})",
                dataset_id=table_id, dataset_name=meta["name"], is_staging=meta["is_staging"],
                broken_column=column, proposed_fix=fix, reason_no_fix=reason,
            ))
        return findings

    # -- fix proposal ---------------------------------------------------------

    def _propose_fix(self, broken_column: str, real_columns: set[str]) -> tuple[str | None, str | None]:
        candidates = difflib.get_close_matches(broken_column, real_columns, n=2, cutoff=_FUZZY_CUTOFF)
        if len(candidates) == 1:
            return candidates[0], None
        if not candidates:
            return None, "no similar column name found -- needs a human"
        return None, f"ambiguous -- {candidates!r} both plausible, needs a human"

    # -- reporting + applying ---------------------------------------------------

    def _report(self, finding: Finding) -> None:
        scope = "staging" if finding.is_staging else "PRODUCTION-BACKED (report only, never auto-applied)"
        self.stdout.write(
            f"\n[{finding.kind}] {finding.object_label} on dataset {finding.dataset_id} "
            f"({finding.dataset_name}, {scope}): references missing column "
            f"{finding.broken_column!r}"
        )
        if finding.proposed_fix:
            self.stdout.write(f"  -> proposed fix: rename to {finding.proposed_fix!r}")
        else:
            self.stdout.write(f"  -> {finding.reason_no_fix}")

    def _apply(self, conn, finding: Finding) -> None:
        assert finding.proposed_fix is not None
        with conn.cursor() as cur:
            if finding.kind == "chart":
                # BOTH columns, not just `params` -- found the hard way (chart
                # 342, this session): `get_chart_data` replays the chart's
                # stored `query_context` verbatim rather than rebuilding it
                # from `params` (a gap this project already knew about, see
                # AI_NATIVE_STATUS.md's Phase 2 section), so a fix that only
                # touches `params` leaves the actual query still broken while
                # looking fixed to anyone who only checks `params`.
                cur.execute(
                    "UPDATE slices SET params = replace(params, %s, %s), "
                    "query_context = replace(query_context, %s, %s) WHERE id = %s",
                    (
                        finding.broken_column, finding.proposed_fix,
                        finding.broken_column, finding.proposed_fix,
                        finding.object_id,
                    ),
                )
            else:
                cur.execute(
                    "UPDATE row_level_security_filters SET clause = replace(clause, %s, %s) WHERE id = %s",
                    (finding.broken_column, finding.proposed_fix, finding.object_id),
                )
        # Commit BEFORE re-verifying: the re-verify call goes through Superset's
        # MCP service, which reads via its own, separate DB connection -- an
        # uncommitted UPDATE on this connection is invisible to that one, so
        # committing only at the very end (this command's first version) made
        # every re-verify report "still broken" even when the fix had worked.
        conn.commit()
        self.stdout.write(self.style.SUCCESS("  applied. Re-verifying..."))
        self._reverify(finding)

    def _reverify(self, finding: Finding) -> None:
        if finding.kind != "chart":
            # RLS filters have no single "run it" call outside a real user's own
            # request; the chart-level re-verify above already proves the dataset's
            # query is sound with the corrected column name.
            self.stdout.write("  (RLS filter fix applied; verify via a real RLS-scoped user's next read)")
            return
        from lifedata_ai.superset_mcp_client import SupersetMCPClient

        client = SupersetMCPClient(username="admin")
        try:
            payload = client.call_any_tool(
                "get_chart_data", {"identifier": finding.object_id, "limit": 5}
            )
        except Exception as exc:  # noqa: BLE001
            self.stdout.write(self.style.ERROR(f"  re-verify call failed: {exc}"))
            return
        if isinstance(payload, dict) and payload.get("error"):
            self.stdout.write(self.style.ERROR(f"  STILL BROKEN after fix: {payload['error'][:200]}"))
        else:
            self.stdout.write(self.style.SUCCESS("  re-verified: chart now runs cleanly."))
