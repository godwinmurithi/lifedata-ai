-- OPTIONAL speed-up for the demo (run by a person with write access to the staging GeoNode database).
-- Superset spends ~1 s per query parsing dataset 251's packed virtual SQL; a physical table makes each
-- card query ~0.2 s. Same 420 synthetic rows. After running it, point dataset 251 at the table:
--   Superset > Datasets > hdss_template_demo > Edit > Source: Physical, table public.hdss_template_demo
-- (bindings.yaml needs no change: same dataset id and columns).
CREATE TABLE public.hdss_template_demo AS
SELECT * FROM (
  -- paste the body of dataset_251_hdss_template_demo.sql here (everything after the first comment line)
) AS demo;
