-- Master dashboard: persons from the registered HDSS site datasets (SINCEP + Navrongo), one row per person,
-- no identifiers (household key is a one-way hash). Age bands as defined for the template: 0-4, 5-14, 15-24, 25-44, 45-64, 65+.
SELECT site, village, district, md5(site || '|' || household_id::text) AS household_key, sex,
       CASE WHEN age IS NULL THEN NULL WHEN age < 5 THEN '0-4' WHEN age < 15 THEN '5-14' WHEN age < 25 THEN '15-24'
            WHEN age < 45 THEN '25-44' WHEN age < 65 THEN '45-64' ELSE '65+' END AS age_band,
       CASE WHEN NULLIF(TRIM(exit_type), '') IS NULL THEN 'Yes' ELSE 'No' END AS resident
FROM (
  SELECT 'SINCEP Iganga-Mayuge HDSS'::text AS site, village, district, household_id::text AS household_id, sex,
         CAST(age_years AS NUMERIC) AS age, exit_type::text AS exit_type
  FROM public.sincep_hdss
  UNION ALL
  SELECT 'Navrongo HDSS', community, NULL, household_id::text, sex,
         CAST(NULLIF(TRIM(age_years::text), '') AS NUMERIC), exit_type::text
  FROM public.navrongo_hdss
) AS p
