-- Sentinel signals (illustrative, not real alerts). Synthetic (docs/design/master/sample_data.json). Not real people or real surveillance.
SELECT
  CAST(NULLIF(c[1], '') AS INTEGER) AS signal_key,
  CAST(NULLIF(c[2], '') AS INTEGER) AS site_key,
  NULLIF(c[3], '') AS signal_label,
  NULLIF(c[4], '') AS syndrome,
  NULLIF(c[5], '') AS severity,
  NULLIF(c[6], '') AS detected
FROM (SELECT string_to_array(rec, chr(31)) AS c FROM unnest(string_to_array('00Febrile illness clusterFeverHigh2h ago11Acute watery diarrheaDiarrheaHigh5h ago22Respiratory surgeRespiratoryModerate1d ago33Rash / suspected measlesRashModerate1d ago44Unexplained feverFeverLow2d ago55Routine reportingLow3d ago66Unexplained feverFeverLow4d ago', chr(30))) AS t(rec)) AS packed