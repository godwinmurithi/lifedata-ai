-- HDSS template source for SINCEP (Iganga-Mayuge). One row per person; no person
-- identifiers, names or free text. Household key is a one-way hash for counting only.
SELECT md5(household_id::text) AS household_key,
       'SINCEP Iganga-Mayuge HDSS'::text AS hdss_site,
       district, county, subcounty, parish, village,
       ST_Y(geometry) AS latitude, ST_X(geometry) AS longitude,
       update_cycle,
       sex, CAST(age_years AS NUMERIC) AS age_years, age_group,
       education_level, occupation, marital_status, residency_status,
       entry_type, exit_type,
       cooking_fuel, water_source, roof_material, floor_material, toilet_type,
       owns_radio, owns_tv, CAST(mobile_phones_owned AS NUMERIC) AS mobile_phones_owned,
       slept_under_net_last_night,
       CAST(collection_date AS DATE) AS collection_date
FROM public.sincep_hdss
