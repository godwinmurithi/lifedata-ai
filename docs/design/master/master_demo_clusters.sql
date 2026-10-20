-- Spatial clusters (illustrative). Synthetic (docs/design/master/sample_data.json). Not real people or real surveillance.
SELECT
  CAST(NULLIF(c[1], '') AS INTEGER) AS village_key,
  CAST(NULLIF(c[2], '') AS INTEGER) AS cases,
  CAST(NULLIF(c[3], '') AS NUMERIC) AS vulnerability,
  CAST(NULLIF(c[4], '') AS NUMERIC) AS radius_km,
  CAST(NULLIF(c[5], '') AS NUMERIC) AS p_value
FROM (SELECT string_to_array(rec, chr(31)) AS c FROM unnest(string_to_array('2638844.20.033231793.80.022027733.10.043719582.90.044216522.40.055514492.20.05518361.90.05476331.60.05595301.30.05', chr(30))) AS t(rec)) AS packed