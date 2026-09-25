-- Synthetic LifeHealth operational schema.
--
-- In the real deployment this data already exists and is out of scope (spec
-- §0). Here we stand up a representative equivalent so the copilot has live
-- metadata and real aggregates to work against.
--
-- Layout mirrors the privacy boundary the spec requires:
--   public.*     — operational tables. Superset has NO access.
--   reporting.*  — flattened views. The ONLY thing Superset sees, and where
--                  small-cell suppression lives so the agent cannot route
--                  around it (spec §1).
--
-- ALL DATA IS SYNTHETIC. No real patients, facilities, or clinical records.

-- ---------------------------------------------------------------------------
-- Operational tables
-- ---------------------------------------------------------------------------

DROP TABLE IF EXISTS public.form_answer CASCADE;
DROP TABLE IF EXISTS public.form_submission CASCADE;
DROP TABLE IF EXISTS public.form_question CASCADE;
DROP TABLE IF EXISTS public.form_version CASCADE;
DROP TABLE IF EXISTS public.dynamic_form CASCADE;
DROP TABLE IF EXISTS public.lab_result CASCADE;
DROP TABLE IF EXISTS public.lab_order CASCADE;
DROP TABLE IF EXISTS public.encounter CASCADE;
DROP TABLE IF EXISTS public.patient CASCADE;
DROP TABLE IF EXISTS public.facility CASCADE;
DROP TABLE IF EXISTS public.district CASCADE;

CREATE TABLE public.district (
    district_id   serial PRIMARY KEY,
    district_name text NOT NULL UNIQUE,
    region        text NOT NULL,
    -- ISO 3166-2:UG code, e.g. 'UG-102' for Kampala.
    --
    -- Superset's Country Map joins its Uganda boundaries on this code and on
    -- nothing else: given district NAMES it draws all 112 districts and fills
    -- none of them, which looks like a rendering bug and is really a failed
    -- join. Carried as a column so the map works without the copilot ever
    -- needing to know that ISO codes exist.
    district_iso  text,
    -- District boundary. Choropleths at this level are the ONLY permitted
    -- rendering of patient geography (spec §6.4).
    boundary      geometry(Polygon, 4326) NOT NULL,
    centroid      geometry(Point, 4326) NOT NULL
);
CREATE INDEX district_boundary_gix ON public.district USING GIST (boundary);

CREATE TABLE public.facility (
    facility_id    serial PRIMARY KEY,
    facility_name  text NOT NULL,
    facility_level text NOT NULL,          -- HC II | HC III | HC IV | Hospital
    district_id    integer NOT NULL REFERENCES public.district(district_id),
    -- Facilities are institutions, not people: exact coordinates are fine and
    -- the registry marks them `geography: facility` (spec §6.4).
    location       geometry(Point, 4326) NOT NULL
);
CREATE INDEX facility_location_gix ON public.facility USING GIST (location);

CREATE TABLE public.patient (
    patient_id   serial PRIMARY KEY,
    sex          text NOT NULL CHECK (sex IN ('female', 'male')),
    birth_year   integer NOT NULL,
    district_id  integer NOT NULL REFERENCES public.district(district_id),
    -- Jittered within the district. NEVER exposed as a point — the reporting
    -- view aggregates to district and the validator rejects point maps on it.
    home_point   geometry(Point, 4326) NOT NULL,
    registered_on date NOT NULL
);
CREATE INDEX patient_home_gix ON public.patient USING GIST (home_point);
CREATE INDEX patient_district_ix ON public.patient (district_id);

CREATE TABLE public.encounter (
    encounter_id   serial PRIMARY KEY,
    patient_id     integer NOT NULL REFERENCES public.patient(patient_id),
    facility_id    integer NOT NULL REFERENCES public.facility(facility_id),
    encounter_date date NOT NULL,
    encounter_type text NOT NULL,          -- outpatient | antenatal | immunisation | emergency
    completed      boolean NOT NULL DEFAULT true,
    diagnosis      text,
    -- Suspected vs confirmed is a distinction the assistant must never blur
    -- (spec §5 system prompt hard rules).
    diagnosis_certainty text CHECK (diagnosis_certainty IN ('suspected', 'confirmed'))
);
CREATE INDEX encounter_date_ix ON public.encounter (encounter_date);
CREATE INDEX encounter_facility_ix ON public.encounter (facility_id);

CREATE TABLE public.lab_order (
    lab_order_id  serial PRIMARY KEY,
    encounter_id  integer NOT NULL REFERENCES public.encounter(encounter_id),
    facility_id   integer NOT NULL REFERENCES public.facility(facility_id),
    test_type     text NOT NULL,           -- malaria_rdt | hiv | tb | cbc | glucose
    ordered_at    timestamptz NOT NULL,
    collected_at  timestamptz,
    resulted_at   timestamptz,
    verified_at   timestamptz,
    -- Order ≠ completed test. TAT is only valid where verified_at is set.
    order_status  text NOT NULL CHECK (order_status IN
                    ('ordered','collected','resulted','verified','cancelled','rejected'))
);
CREATE INDEX lab_order_ordered_ix ON public.lab_order (ordered_at);
CREATE INDEX lab_order_status_ix ON public.lab_order (order_status);

CREATE TABLE public.lab_result (
    lab_result_id serial PRIMARY KEY,
    lab_order_id  integer NOT NULL REFERENCES public.lab_order(lab_order_id),
    result_value  text NOT NULL,           -- positive | negative | indeterminate
    numeric_value numeric
);

-- --- Dynamic forms (spec §5: wire one or two, don't build a generic flattener)

CREATE TABLE public.dynamic_form (
    form_id   serial PRIMARY KEY,
    form_key  text NOT NULL UNIQUE,
    form_name text NOT NULL
);

CREATE TABLE public.form_version (
    form_version_id serial PRIMARY KEY,
    form_id         integer NOT NULL REFERENCES public.dynamic_form(form_id),
    version_number  integer NOT NULL,
    published_on    date NOT NULL
);

CREATE TABLE public.form_question (
    question_id     serial PRIMARY KEY,
    form_version_id integer NOT NULL REFERENCES public.form_version(form_version_id),
    question_key    text NOT NULL,
    question_label  text NOT NULL,
    question_type   text NOT NULL,         -- integer | decimal | boolean | select | date
    unit            text,
    is_sensitive    boolean NOT NULL DEFAULT false,
    display_order   integer NOT NULL
);

CREATE TABLE public.form_submission (
    submission_id   serial PRIMARY KEY,
    form_version_id integer NOT NULL REFERENCES public.form_version(form_version_id),
    facility_id     integer NOT NULL REFERENCES public.facility(facility_id),
    submitted_on    date NOT NULL
);

CREATE TABLE public.form_answer (
    answer_id     serial PRIMARY KEY,
    submission_id integer NOT NULL REFERENCES public.form_submission(submission_id),
    question_id   integer NOT NULL REFERENCES public.form_question(question_id),
    value_text    text,
    value_number  numeric,
    value_bool    boolean
);
CREATE INDEX form_answer_submission_ix ON public.form_answer (submission_id);
