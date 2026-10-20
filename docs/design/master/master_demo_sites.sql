-- Sentinel sites. Synthetic (docs/design/master/sample_data.json). Not real people or real surveillance.
SELECT
  CAST(NULLIF(c[1], '') AS INTEGER) AS site_key,
  NULLIF(c[2], '') AS facility,
  CAST(NULLIF(c[3], '') AS INTEGER) AS village_key,
  NULLIF(c[4], '') AS site_status,
  CAST(NULLIF(c[5], '') AS INTEGER) AS reports,
  CAST(NULLIF(c[6], '') AS INTEGER) AS rrt_deployments,
  CAST(NULLIF(c[7], '') AS NUMERIC) AS reporting_lag_days
FROM (SELECT string_to_array(rec, chr(31)) AS c FROM unnest(string_to_array('0Buikwe HC III42alert1711.2981Lugazi HC III20alert1511.1952Kawolo HC IV65alert1901.0123Naminya HC II37alert1411.5484Ngogwe HC II51watch901.5835Wakisii HC II38watch1200.836Njeru HC III26watch1001.6517Nyenga Hospital32watch1501.2838Kavule HC III62watch1501.4989Najjemba HC59watch1501.8510Makindu HC III55watch1301.35111Nkokonjeru Hospital47normal1001.36912St. Charles Buikwe Hospital43normal801.84213Kabizzi HC II33normal1401.6614Kizigo HC II60normal1201.87315Bugungu HC II27normal1501.01316Canaan HC II28normal1301.50717Kisimbi Moslem HC III56normal1401.63718Konko HC II39normal1101.35919Kidron HC II40normal1100.95820Lugazi Mission21normal1501.56921Jehovah Rapha HC III34normal1301.36522Nukaya HC III29normal1101.69323Tongolo HC II35normal1100.994', chr(30))) AS t(rec)) AS packed