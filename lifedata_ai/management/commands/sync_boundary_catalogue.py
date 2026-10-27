"""Download, validate and retain African geoBoundaries releases locally."""

import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from lifedata_ai.boundary_catalogue import (
    AFRICAN_COUNTRIES,
    AFRICAN_TERRITORIES,
    cache_root,
    coverage_manifest,
    import_layer,
    source_index,
)


class Command(BaseCommand):
    help = "Synchronize versioned African administrative boundaries (does not rewrite dashboards)."

    def add_arguments(self, parser):
        parser.add_argument("--countries", nargs="+")
        parser.add_argument("--levels", nargs="+", type=int, default=[0, 1, 2])
        parser.add_argument("--refresh-index", action="store_true")
        parser.add_argument("--workers", type=int, default=1)
        parser.add_argument("--retry", action="store_true")

    def handle(self, *args, **options):
        countries = set(options["countries"] or (AFRICAN_COUNTRIES | AFRICAN_TERRITORIES))
        index = source_index(options["refresh_index"])
        layers = [
            m
            for m in index
            if m["boundaryISO"] in countries and int(m["boundaryType"][3:]) in options["levels"]
        ]
        layers.sort(key=lambda m: (m["boundaryType"], m["boundaryISO"]))
        self.stdout.write(
            f"{len(layers)} available layers selected; missing levels remain in coverage manifest."
        )

        def work(meta):
            close_old_connections()
            try:
                release, changed = import_layer(meta, retry=options["retry"])
                return f"{release.pk}: {release.feature_count} areas ({'imported' if changed else 'cached'})"
            except Exception as exc:
                return f"{meta['boundaryID']}: FAILED {exc}"
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=max(1, min(options["workers"], 6))) as pool:
            futures = [pool.submit(work, m) for m in layers]
            for future in as_completed(futures):
                self.stdout.write(future.result())
                self.stdout.flush()
        report = coverage_manifest()
        (cache_root() / "coverage.json").write_text(json.dumps(report, indent=2))
        self.stdout.write(
            f"{report['countries_ready']}/{report['countries_expected']} countries have validated ADM0; "
            f"{len(report['missing_core_levels'])} missing ADM0–ADM2 country/level combinations."
        )
