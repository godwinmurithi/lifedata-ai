-- Master dashboard: vital events from the registered HDSS site datasets. Entry Birth / In-migration (entry_date),
-- exit Death / Out-migration (exit_date). No identifiers.
SELECT site, village, event_type, event_date FROM (
  SELECT 'SINCEP Iganga-Mayuge HDSS'::text AS site, village,
         CASE entry_type WHEN 'Birth' THEN 'Births' WHEN 'In-migration' THEN 'In-migration' END AS event_type,
         CAST(NULLIF(TRIM(entry_date::text), '') AS DATE) AS event_date
  FROM public.sincep_hdss WHERE entry_type IN ('Birth', 'In-migration')
  UNION ALL
  SELECT 'SINCEP Iganga-Mayuge HDSS', village,
         CASE exit_type WHEN 'Death' THEN 'Deaths' WHEN 'Out-migration' THEN 'Out-migration' END,
         CAST(NULLIF(TRIM(exit_date::text), '') AS DATE)
  FROM public.sincep_hdss WHERE exit_type IN ('Death', 'Out-migration')
  UNION ALL
  SELECT 'Navrongo HDSS', community,
         CASE entry_type WHEN 'Birth' THEN 'Births' WHEN 'In-migration' THEN 'In-migration' END,
         CAST(NULLIF(TRIM(entry_date::text), '') AS DATE)
  FROM public.navrongo_hdss WHERE entry_type IN ('Birth', 'In-migration')
  UNION ALL
  SELECT 'Navrongo HDSS', community,
         CASE exit_type WHEN 'Death' THEN 'Deaths' WHEN 'Out-migration' THEN 'Out-migration' END,
         CAST(NULLIF(TRIM(exit_date::text), '') AS DATE)
  FROM public.navrongo_hdss WHERE exit_type IN ('Death', 'Out-migration')
) AS e
