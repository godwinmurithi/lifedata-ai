-- Two logical databases on one PostGIS-enabled Postgres instance:
--
--   lifedata  — LifeHealth operational tables, the flattened `reporting` views,
--               and the Django orchestrator's own tables. Superset connects to
--               this as an *analytics database*, read-only.
--   superset  — Superset's own Flask-AppBuilder metadata (users, roles, charts,
--               dashboards). Never queried by Django.
--
-- `lifedata` is created by POSTGRES_DB; only the metadata DB is created here.

CREATE DATABASE superset;

-- A read-only login for Superset's connection to the analytics database.
-- Superset must never be able to write to LifeHealth data.
CREATE ROLE superset_ro LOGIN PASSWORD 'superset-ro-dev-password';

\connect lifedata

-- PostGIS. Facility locations are exact points; patient origins are only ever
-- exposed aggregated to district level (spec §1 privacy boundary, §6.4).
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS postgis_topology;

-- Raw operational tables live in `public`; Superset never sees them.
-- `reporting` holds the flattened views that ARE exposed, and is where
-- small-cell suppression is enforced (spec §1: in the views themselves, not in
-- runtime code the agent could route around).
CREATE SCHEMA IF NOT EXISTS reporting;

GRANT CONNECT ON DATABASE lifedata TO superset_ro;
GRANT USAGE ON SCHEMA reporting TO superset_ro;

-- Anything created later in `reporting` is readable by Superset automatically.
ALTER DEFAULT PRIVILEGES IN SCHEMA reporting GRANT SELECT ON TABLES TO superset_ro;

-- PostGIS puts its metadata in public; Superset needs to read spatial_ref_sys
-- to interpret geometry, but nothing else in that schema.
GRANT USAGE ON SCHEMA public TO superset_ro;
GRANT SELECT ON TABLE public.spatial_ref_sys TO superset_ro;

-- Explicitly withhold everything else in public: the operational tables are
-- off-limits, so the copilot cannot reach row-level records even if it tried.
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM superset_ro;
