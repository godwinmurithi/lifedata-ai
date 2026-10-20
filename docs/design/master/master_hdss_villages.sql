-- Master dashboard: villages = distinct (site, district, village) across the registered HDSS site datasets.
-- No coordinates: SINCEP records share one point and Navrongo's are mostly the site point (needs definition).
SELECT DISTINCT site, district, village FROM (
  SELECT 'SINCEP Iganga-Mayuge HDSS'::text AS site, district, village FROM public.sincep_hdss
  UNION ALL
  SELECT 'Navrongo HDSS', NULL, community FROM public.navrongo_hdss
) AS v WHERE village IS NOT NULL
