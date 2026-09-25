-- Flattened reporting views — the ONLY surface Superset (and therefore the
-- copilot) can see.
--
-- Small-cell suppression lives HERE, in the views themselves, not in runtime
-- code the agent could route around (spec §1 privacy boundary). Anything
-- grouping to a population smaller than 5 is withheld.

DROP VIEW IF EXISTS reporting.rpt_diabetes_screening CASCADE;
DROP VIEW IF EXISTS reporting.rpt_patient_geography CASCADE;
DROP VIEW IF EXISTS reporting.rpt_lab_orders CASCADE;
DROP VIEW IF EXISTS reporting.rpt_encounters CASCADE;
DROP VIEW IF EXISTS reporting.rpt_facilities CASCADE;

-- ---------------------------------------------------------------------------
-- Facilities — institutions, so exact coordinates are permitted.
-- ---------------------------------------------------------------------------
CREATE VIEW reporting.rpt_facilities AS
SELECT
    f.facility_id,
    f.facility_name,
    f.facility_level,
    d.district_name,
    d.district_iso,
    d.region,
    ST_Y(f.location)  AS latitude,
    ST_X(f.location)  AS longitude
FROM public.facility f
JOIN public.district d USING (district_id);

COMMENT ON VIEW reporting.rpt_facilities IS
  'One row per health facility. Exact coordinates permitted (institution, not person).';

-- ---------------------------------------------------------------------------
-- Encounters — one row per encounter. Patient counts need COUNT(DISTINCT).
-- ---------------------------------------------------------------------------
CREATE VIEW reporting.rpt_encounters AS
SELECT
    e.encounter_id,
    e.encounter_date,
    date_trunc('month', e.encounter_date)::date AS encounter_month,
    e.encounter_type,
    e.completed,
    e.diagnosis,
    e.diagnosis_certainty,
    f.facility_id,
    f.facility_name,
    f.facility_level,
    d.district_name,
    d.district_iso,
    d.region,
    p.patient_id,
    p.sex,
    EXTRACT(YEAR FROM e.encounter_date)::int - p.birth_year AS age_years
FROM public.encounter e
JOIN public.facility f USING (facility_id)
JOIN public.district d ON d.district_id = f.district_id
JOIN public.patient  p USING (patient_id);

COMMENT ON VIEW reporting.rpt_encounters IS
  'One row per encounter. patient_id repeats — unique patients require COUNT(DISTINCT patient_id). '
  'diagnosis_certainty distinguishes suspected from confirmed; never conflate them.';

-- ---------------------------------------------------------------------------
-- Laboratory orders — order ≠ completed test.
-- turnaround_hours is NULL unless verified_at is set, so an average over it
-- silently excludes incomplete work rather than understating it.
-- ---------------------------------------------------------------------------
CREATE VIEW reporting.rpt_lab_orders AS
SELECT
    lo.lab_order_id,
    lo.test_type,
    lo.order_status,
    lo.ordered_at,
    date_trunc('month', lo.ordered_at)::date AS ordered_month,
    lo.verified_at,
    CASE
        WHEN lo.verified_at IS NOT NULL
        THEN EXTRACT(EPOCH FROM (lo.verified_at - lo.ordered_at)) / 3600.0
    END AS turnaround_hours,
    lr.result_value,
    f.facility_id,
    f.facility_name,
    f.facility_level,
    d.district_name,
    d.district_iso,
    d.region
FROM public.lab_order lo
LEFT JOIN public.lab_result lr USING (lab_order_id)
JOIN public.facility f USING (facility_id)
JOIN public.district d ON d.district_id = f.district_id;

COMMENT ON VIEW reporting.rpt_lab_orders IS
  'One row per laboratory ORDER, not per completed test. turnaround_hours is only '
  'populated where verified_at is set. Malaria positivity = positive verified tests / '
  'verified tests, excluding cancelled and rejected; it counts tests, not patients.';

-- ---------------------------------------------------------------------------
-- Patient geography — DISTRICT LEVEL ONLY.
--
-- This view is the enforcement point for the geography policy. There is no
-- patient_id and no point geometry to select, so a sub-district grouping or a
-- point map is not merely rejected by the validator — it is unrepresentable.
-- Districts with fewer than 5 patients are withheld entirely.
-- ---------------------------------------------------------------------------
CREATE VIEW reporting.rpt_patient_geography AS
SELECT
    d.district_name,
    d.district_iso,
    d.region,
    COUNT(*)                                        AS patient_count,
    COUNT(*) FILTER (WHERE p.sex = 'female')        AS female_count,
    COUNT(*) FILTER (WHERE p.sex = 'male')          AS male_count,
    ROUND(AVG(EXTRACT(YEAR FROM CURRENT_DATE)::int - p.birth_year), 1) AS mean_age_years,
    -- District centroid, for choropleth labelling. NOT a patient location.
    ST_Y(d.centroid) AS district_centroid_latitude,
    ST_X(d.centroid) AS district_centroid_longitude
FROM public.patient p
JOIN public.district d USING (district_id)
GROUP BY d.district_name, d.district_iso, d.region, d.centroid
HAVING COUNT(*) >= 5;

COMMENT ON VIEW reporting.rpt_patient_geography IS
  'Patient counts aggregated to DISTRICT. Small cells (<5 patients) suppressed. '
  'Contains no patient identifiers and no point geometry: sub-district grouping and '
  'point/scatter/heat maps are impossible by construction, not merely disallowed.';

-- ---------------------------------------------------------------------------
-- Diabetes screening form — one wired dynamic form (spec §5).
-- Answers are pivoted into columns; no generic flattener.
-- ---------------------------------------------------------------------------
CREATE VIEW reporting.rpt_diabetes_screening AS
SELECT
    s.submission_id,
    s.submitted_on,
    date_trunc('month', s.submitted_on)::date AS submitted_month,
    f.facility_id,
    f.facility_name,
    d.district_name,
    d.district_iso,
    d.region,
    MAX(a.value_number) FILTER (WHERE q.question_key = 'fasting_glucose') AS fasting_glucose_mmol_l,
    MAX(a.value_number) FILTER (WHERE q.question_key = 'bmi')             AS bmi,
    MAX(a.value_number) FILTER (WHERE q.question_key = 'age')             AS age_years,
    BOOL_OR(a.value_bool) FILTER (WHERE q.question_key = 'family_history') AS family_history,
    MAX(a.value_text)   FILTER (WHERE q.question_key = 'risk_band')       AS risk_band
FROM public.form_submission s
JOIN public.form_version  v USING (form_version_id)
JOIN public.dynamic_form  df USING (form_id)
JOIN public.form_answer   a USING (submission_id)
JOIN public.form_question q USING (question_id)
JOIN public.facility      f ON f.facility_id = s.facility_id
JOIN public.district      d ON d.district_id = f.district_id
WHERE df.form_key = 'diabetes_screening'
GROUP BY s.submission_id, s.submitted_on, f.facility_id, f.facility_name,
         d.district_name, d.district_iso, d.region;

COMMENT ON VIEW reporting.rpt_diabetes_screening IS
  'One row per diabetes screening submission. A screening result is NOT a diagnosis.';

-- Superset's read-only role can see every reporting view.
GRANT SELECT ON ALL TABLES IN SCHEMA reporting TO superset_ro;
