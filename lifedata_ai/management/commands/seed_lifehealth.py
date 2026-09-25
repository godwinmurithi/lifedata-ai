"""Generate synthetic LifeHealth data and the reporting views Superset reads.

In a real deployment this data already exists and is out of scope (spec §0).
Here it gives the copilot live metadata and genuine aggregates to reason about.

Everything is synthetic and reproducible: a fixed seed means the same database
every run, so demos and tests are stable.

    python manage.py seed_lifehealth
    python manage.py seed_lifehealth --patients 4000 --months 24 --seed 7
"""

from __future__ import annotations

import datetime as dt
import math
import random
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import connection, transaction

SQL_DIR = Path(__file__).resolve().parent.parent.parent / "sql"

# Real district names (public geographic entities); ALL health data is invented.
#
# The ISO 3166-2:UG code is what Superset's Country Map joins its Uganda
# boundaries on. Taken from the boundary set Superset itself ships — the codes
# were read out of its own GeoJSON rather than looked up, because the map only
# fills districts whose code matches ITS data, and a code that is correct in the
# abstract but absent there fills nothing.
#
# `Fort Portal` maps to Kabarole (UG-405): Fort Portal is the city, Kabarole the
# district that Superset's boundaries still carry. The two were separated in
# 2020 and that boundary set predates the split.
DISTRICTS = [
    # name,      region,      centre lon,  centre lat, span,  ISO 3166-2
    ("Kampala", "Central", 32.582, 0.347, 0.10, "UG-102"),
    ("Wakiso", "Central", 32.459, 0.404, 0.22, "UG-113"),
    ("Mukono", "Central", 32.755, 0.353, 0.20, "UG-108"),
    ("Jinja", "Eastern", 33.204, 0.424, 0.16, "UG-204"),
    ("Mbale", "Eastern", 34.175, 1.082, 0.18, "UG-209"),
    ("Soroti", "Eastern", 33.611, 1.715, 0.20, "UG-211"),
    ("Gulu", "Northern", 32.299, 2.774, 0.24, "UG-304"),
    ("Lira", "Northern", 32.900, 2.249, 0.20, "UG-307"),
    ("Arua", "Northern", 30.911, 3.020, 0.22, "UG-303"),
    ("Mbarara", "Western", 30.658, -0.607, 0.20, "UG-410"),
    ("Fort Portal", "Western", 30.275, 0.671, 0.18, "UG-405"),
    ("Hoima", "Western", 31.352, 1.435, 0.22, "UG-403"),
]

FACILITY_LEVELS = ["HC II", "HC III", "HC IV", "Hospital"]
ENCOUNTER_TYPES = ["outpatient", "antenatal", "immunisation", "emergency"]
DIAGNOSES = [
    "malaria", "upper respiratory infection", "hypertension", "diabetes mellitus",
    "diarrhoeal disease", "pneumonia", "anaemia", "urinary tract infection",
]
TEST_TYPES = ["malaria_rdt", "hiv", "tb", "cbc", "glucose"]


class Command(BaseCommand):
    help = "Create synthetic LifeHealth data and the reporting views Superset reads."

    def add_arguments(self, parser):
        parser.add_argument("--patients", type=int, default=2500)
        parser.add_argument("--months", type=int, default=18)
        parser.add_argument("--seed", type=int, default=20260101)
        parser.add_argument(
            "--views-only",
            action="store_true",
            help="Rebuild the reporting views without regenerating data.",
        )

    def handle(self, *args, **options):
        rng = random.Random(options["seed"])

        if options["views_only"]:
            self._run_sql_file("reporting_views.sql")
            self.stdout.write(self.style.SUCCESS("Reporting views rebuilt."))
            return

        self.stdout.write("Creating operational schema…")
        self._run_sql_file("lifehealth_schema.sql")

        with transaction.atomic():
            districts = self._seed_districts()
            facilities = self._seed_facilities(rng, districts)
            patients = self._seed_patients(rng, districts, options["patients"])
            encounters = self._seed_encounters(rng, patients, facilities, options["months"])
            self._seed_labs(rng, encounters)
            self._seed_forms(rng, facilities, options["months"])

        self.stdout.write("Building reporting views…")
        self._run_sql_file("reporting_views.sql")

        self._report()

    # -- helpers ----------------------------------------------------------

    def _run_sql_file(self, name: str) -> None:
        sql = (SQL_DIR / name).read_text(encoding="utf-8")
        with connection.cursor() as cur:
            cur.execute(sql)

    def _seed_districts(self) -> list[int]:
        rows = []
        for name, region, lon, lat, span, iso in DISTRICTS:
            half = span / 2
            # A simple rectangular boundary is enough for a choropleth demo.
            polygon = (
                f"POLYGON(({lon - half} {lat - half}, {lon + half} {lat - half}, "
                f"{lon + half} {lat + half}, {lon - half} {lat + half}, "
                f"{lon - half} {lat - half}))"
            )
            rows.append((name, region, iso, polygon, f"POINT({lon} {lat})"))

        with connection.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO public.district
                       (district_name, region, district_iso, boundary, centroid)
                VALUES (%s, %s, %s, ST_GeomFromText(%s, 4326), ST_GeomFromText(%s, 4326))
                """,
                rows,
            )
            cur.execute("SELECT district_id FROM public.district ORDER BY district_id")
            ids = [r[0] for r in cur.fetchall()]
        self.stdout.write(f"  districts: {len(ids)}")
        return ids

    def _seed_facilities(self, rng: random.Random, districts: list[int]) -> list[tuple[int, int]]:
        rows = []
        for district_id, (name, _region, lon, lat, span, _iso) in zip(
            districts, DISTRICTS, strict=True
        ):
            for i in range(rng.randint(3, 6)):
                level = rng.choices(FACILITY_LEVELS, weights=[40, 35, 15, 10])[0]
                flon = lon + rng.uniform(-span / 3, span / 3)
                flat = lat + rng.uniform(-span / 3, span / 3)
                rows.append(
                    (f"{name} {level} #{i + 1}", level, district_id, f"POINT({flon} {flat})")
                )

        with connection.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO public.facility
                    (facility_name, facility_level, district_id, location)
                VALUES (%s, %s, %s, ST_GeomFromText(%s, 4326))
                """,
                rows,
            )
            cur.execute("SELECT facility_id, district_id FROM public.facility")
            facilities = cur.fetchall()
        self.stdout.write(f"  facilities: {len(facilities)}")
        return facilities

    def _seed_patients(
        self, rng: random.Random, districts: list[int], count: int
    ) -> list[tuple[int, int]]:
        today = dt.date.today()
        rows = []
        for _ in range(count):
            idx = rng.randrange(len(districts))
            district_id = districts[idx]
            _name, _region, lon, lat, span, _iso = DISTRICTS[idx]
            # Jitter inside the district. Never exposed as a point.
            plon = lon + rng.uniform(-span / 2.2, span / 2.2)
            plat = lat + rng.uniform(-span / 2.2, span / 2.2)
            rows.append(
                (
                    rng.choice(["female", "male"]),
                    rng.randint(today.year - 85, today.year - 1),
                    district_id,
                    f"POINT({plon} {plat})",
                    today - dt.timedelta(days=rng.randint(0, 1500)),
                )
            )

        with connection.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO public.patient
                    (sex, birth_year, district_id, home_point, registered_on)
                VALUES (%s, %s, %s, ST_GeomFromText(%s, 4326), %s)
                """,
                rows,
            )
            cur.execute("SELECT patient_id, district_id FROM public.patient")
            patients = cur.fetchall()
        self.stdout.write(f"  patients: {len(patients)}")
        return patients

    def _seed_encounters(
        self,
        rng: random.Random,
        patients: list[tuple[int, int]],
        facilities: list[tuple[int, int]],
        months: int,
    ) -> list[tuple[int, int, dt.date]]:
        by_district: dict[int, list[int]] = {}
        for facility_id, district_id in facilities:
            by_district.setdefault(district_id, []).append(facility_id)

        today = dt.date.today()
        start = today - dt.timedelta(days=months * 30)
        rows = []
        for patient_id, district_id in patients:
            for _ in range(rng.randint(1, 6)):
                offset = rng.randrange(months * 30)
                date = start + dt.timedelta(days=offset)
                # Seasonal malaria: peaks around the rainy months.
                seasonal = 0.5 + 0.5 * math.sin((date.month / 12) * 2 * math.pi)
                if rng.random() < 0.35 * seasonal + 0.1:
                    diagnosis = "malaria"
                else:
                    diagnosis = rng.choice(DIAGNOSES)
                certainty = rng.choices(["confirmed", "suspected"], weights=[65, 35])[0]
                rows.append(
                    (
                        patient_id,
                        rng.choice(by_district[district_id]),
                        date,
                        rng.choices(ENCOUNTER_TYPES, weights=[70, 12, 12, 6])[0],
                        rng.random() > 0.05,
                        diagnosis,
                        certainty,
                    )
                )

        with connection.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO public.encounter
                    (patient_id, facility_id, encounter_date, encounter_type,
                     completed, diagnosis, diagnosis_certainty)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                rows,
            )
            cur.execute(
                "SELECT encounter_id, facility_id, encounter_date FROM public.encounter"
            )
            encounters = cur.fetchall()
        self.stdout.write(f"  encounters: {len(encounters)}")
        return encounters

    def _seed_labs(self, rng: random.Random, encounters: list[tuple[int, int, dt.date]]) -> None:
        orders, results = [], []
        for encounter_id, facility_id, date in encounters:
            if rng.random() > 0.45:
                continue
            test_type = rng.choices(TEST_TYPES, weights=[45, 15, 10, 20, 10])[0]
            ordered = dt.datetime.combine(date, dt.time(rng.randint(8, 16), rng.randint(0, 59)))
            # A realistic mix: most complete, some still in flight, a few dropped.
            status = rng.choices(
                ["verified", "resulted", "collected", "ordered", "cancelled", "rejected"],
                weights=[62, 12, 8, 8, 6, 4],
            )[0]
            collected = ordered + dt.timedelta(minutes=rng.randint(10, 240)) \
                if status not in ("ordered", "cancelled") else None
            resulted = (
                collected + dt.timedelta(hours=rng.randint(1, 40))
                if collected and status in ("resulted", "verified")
                else None
            )
            verified = (
                resulted + dt.timedelta(hours=rng.randint(1, 12))
                if resulted and status == "verified"
                else None
            )
            orders.append(
                (encounter_id, facility_id, test_type, ordered, collected, resulted, verified, status)
            )

        with connection.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO public.lab_order
                    (encounter_id, facility_id, test_type, ordered_at, collected_at,
                     resulted_at, verified_at, order_status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                orders,
            )
            cur.execute(
                "SELECT lab_order_id, test_type FROM public.lab_order "
                "WHERE order_status IN ('resulted', 'verified')"
            )
            for lab_order_id, test_type in cur.fetchall():
                positivity = {"malaria_rdt": 0.32, "hiv": 0.05, "tb": 0.08}.get(test_type, 0.20)
                value = "positive" if rng.random() < positivity else "negative"
                numeric = round(rng.uniform(3.5, 14.0), 1) if test_type == "glucose" else None
                results.append((lab_order_id, value, numeric))
            cur.executemany(
                "INSERT INTO public.lab_result (lab_order_id, result_value, numeric_value) "
                "VALUES (%s, %s, %s)",
                results,
            )
        self.stdout.write(f"  lab orders: {len(orders)} (results: {len(results)})")

    def _seed_forms(
        self, rng: random.Random, facilities: list[tuple[int, int]], months: int
    ) -> None:
        questions = [
            ("age", "Age in years", "integer", "years", False, 1),
            ("bmi", "Body mass index", "decimal", "kg/m2", False, 2),
            ("fasting_glucose", "Fasting glucose", "decimal", "mmol/L", False, 3),
            ("family_history", "Family history of diabetes", "boolean", None, False, 4),
            ("risk_band", "Risk band", "select", None, False, 5),
        ]
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO public.dynamic_form (form_key, form_name) "
                "VALUES ('diabetes_screening', 'Diabetes Screening') RETURNING form_id"
            )
            form_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO public.form_version (form_id, version_number, published_on) "
                "VALUES (%s, 1, %s) RETURNING form_version_id",
                (form_id, dt.date.today() - dt.timedelta(days=months * 30)),
            )
            version_id = cur.fetchone()[0]

            question_ids = {}
            for key, label, qtype, unit, sensitive, order in questions:
                cur.execute(
                    """
                    INSERT INTO public.form_question
                        (form_version_id, question_key, question_label, question_type,
                         unit, is_sensitive, display_order)
                    VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING question_id
                    """,
                    (version_id, key, label, qtype, unit, sensitive, order),
                )
                question_ids[key] = cur.fetchone()[0]

            today = dt.date.today()
            submissions, answers = 0, []
            for _ in range(900):
                facility_id = rng.choice(facilities)[0]
                submitted = today - dt.timedelta(days=rng.randrange(months * 30))
                cur.execute(
                    "INSERT INTO public.form_submission "
                    "(form_version_id, facility_id, submitted_on) "
                    "VALUES (%s, %s, %s) RETURNING submission_id",
                    (version_id, facility_id, submitted),
                )
                submission_id = cur.fetchone()[0]
                submissions += 1

                age = rng.randint(18, 80)
                bmi = round(rng.uniform(17.0, 39.0), 1)
                glucose = round(rng.uniform(4.0, 12.5), 1)
                history = rng.random() < 0.28
                band = (
                    "high" if glucose >= 7.0 or bmi >= 32
                    else "moderate" if glucose >= 6.1 or bmi >= 27
                    else "low"
                )
                answers.extend([
                    (submission_id, question_ids["age"], None, age, None),
                    (submission_id, question_ids["bmi"], None, bmi, None),
                    (submission_id, question_ids["fasting_glucose"], None, glucose, None),
                    (submission_id, question_ids["family_history"], None, None, history),
                    (submission_id, question_ids["risk_band"], band, None, None),
                ])

            cur.executemany(
                "INSERT INTO public.form_answer "
                "(submission_id, question_id, value_text, value_number, value_bool) "
                "VALUES (%s, %s, %s, %s, %s)",
                answers,
            )
        self.stdout.write(f"  form submissions: {submissions}")

    def _report(self) -> None:
        with connection.cursor() as cur:
            self.stdout.write(self.style.SUCCESS("\nReporting views (what Superset sees):"))
            for view in (
                "rpt_facilities",
                "rpt_encounters",
                "rpt_lab_orders",
                "rpt_patient_geography",
                "rpt_diabetes_screening",
            ):
                cur.execute(f"SELECT COUNT(*) FROM reporting.{view}")
                self.stdout.write(f"  reporting.{view:<24} {cur.fetchone()[0]:>7} rows")
        self.stdout.write(
            self.style.WARNING("\nAll data is synthetic. No real patients or facilities.")
        )
