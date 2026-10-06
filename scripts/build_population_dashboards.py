"""Build NEW Superset population dashboards using the existing Sandbox join.

Runs on the dev host with its saved admin session. Never edits the old Sandbox.
The manifest makes subsequent runs update only artifacts created by this script.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FACILITY = "St. Charles Buikwe Hospital"
PREFIX = "LifeHealth Sandbox · "


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-file", default="/tmp/lifedata-author-session.json")
    parser.add_argument("--manifest", default="/tmp/lifedata-population-build.json")
    parser.add_argument("--base-url", default="https://sandbox.lifehealth.app")
    parser.add_argument("--data-only", action="store_true")
    args = parser.parse_args()
    session = requests.Session()
    credentials = json.loads(Path(args.session_file).read_text())
    base = args.base_url.rstrip("/")
    session.cookies.set("session", credentials["session"], domain=urlparse(base).hostname, path="/")

    def api(method, path, body=None):
        response = session.request(method, base + "/api/v1/" + path, json=body, timeout=120)
        if not response.ok:
            raise RuntimeError(f"{method} {path}: {response.status_code} {response.text[:700]}")
        return response.json()

    session.headers.update(
        {"X-CSRFToken": api("GET", "security/csrf_token/")["result"], "Referer": base + "/"}
    )
    credentials["session"] = session.cookies.get(
        "session", domain=urlparse(base).hostname, path="/"
    )
    Path(args.session_file).write_text(json.dumps(credentials))
    Path(args.session_file).chmod(0o600)
    source = api("GET", "dataset/204")["result"]
    facility_result = api(
        "POST",
        "chart/data",
        {
            "datasource": {"id": 18, "type": "table"},
            "queries": [
                {
                    "columns": ["facility_name", "lat", "lng", "subcounty", "facility_level", "beds"],
                    "metrics": [],
                    "row_limit": 10000,
                }
            ],
            "result_format": "json",
            "result_type": "full",
        },
    )["result"][0]
    if facility_result.get("error"):
        raise RuntimeError(facility_result["error"])
    facility_rows = facility_result["data"]
    if not any(r["facility_name"] == DEFAULT_FACILITY for r in facility_rows):
        raise RuntimeError("The selected default facility is missing from the existing census.")
    definitions = json.loads(
        subprocess.check_output(
            [
                "docker",
                "exec",
                "lifedata-ai4cti",
                "/app/.venv/bin/python",
                "-c",
                "import os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','lifedata.settings'); "
                "import django; django.setup(); import json; from lifedata_ai.population import FIELDS,STAGES; "
                "print(json.dumps({'fields':FIELDS,'stages':STAGES}))",
            ],
            text=True,
        )
    )
    fields = {
        k: v
        for k, v in definitions["fields"].items()
        if k in {c["column_name"] for c in source["columns"]} or k == "distance_km"
    }
    manifest_path = Path(args.manifest)
    manifest = (
        json.loads(manifest_path.read_text())
        if manifest_path.exists()
        else {"datasets": {}, "charts": {}, "dashboards": {}, "default_facility": DEFAULT_FACILITY}
    )
    manifest["default_facility"] = ""

    def save():
        manifest_path.write_text(json.dumps(manifest, indent=2))
        manifest_path.chmod(0o600)

    def dataset(key, sql):
        body = {
            "database": source["database"]["id"],
            "schema": "public",
            "table_name": "lifehealth_discovery_" + key,
            "sql": sql,
        }
        if key in manifest["datasets"]:
            identifier = manifest["datasets"][key]
            api("PUT", f"dataset/{identifier}", {"sql": sql})
        else:
            identifier = api("POST", "dataset/", body)["id"]
            manifest["datasets"][key] = identifier
            save()
        return identifier

    # The existing facility survey lives in another Superset database. Reuse
    # its verified coordinate lookup as a small virtual VALUES dataset.
    # The population remains a live SELECT of the original Sandbox join.
    def literal(value):
        return "'" + str(value).replace("'", "''") + "'"

    facility_values = []
    for row in facility_rows:
        lat, lng = float(row["lat"]), float(row["lng"])
        if not (
            math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180
        ):
            continue
        facility_values.append(
            f"({literal(row['facility_name'])},{lat},{lng},{literal(row['subcounty'])},"
            f"{literal(row['facility_level']) if row['facility_level'] is not None else 'NULL'},"
            f"{float(row['beds']) if row['beds'] is not None else 'NULL'})"
        )
    facility_sql = (
        "SELECT * FROM (VALUES "
        + ",".join(facility_values)
        + ") AS locations(facility_name,lat,lng,subcounty,facility_level,beds)"
    )
    facility_id = dataset("facilities", facility_sql)
    expressions = []
    for field, definition in fields.items():
        if field == "distance_km":
            continue
        if definition["kind"] == "number":
            expressions.append(
                f"CASE WHEN btrim(p.\"{field}\"::text) ~ '^[+-]?[0-9]+([.][0-9]+)?$' "
                f'THEN p."{field}"::double precision END AS "{field}"'
            )
        else:
            expressions.append(
                f"COALESCE(NULLIF(btrim(p.\"{field}\"::text),''),'Not recorded') AS \"{field}\""
            )
    population_sql = f"""{{% set discovery_facility_filters = get_filters('facility_name', remove_filter=True) %}}
WITH facilities AS (SELECT * FROM ({facility_sql}) available_facilities WHERE TRUE
{{% for filter in discovery_facility_filters %}}{{% if filter.get('op') == 'IN' %}}
AND facility_name IN {{{{ filter.get('val') | where_in }}}}
{{% endif %}}{{% endfor %}})
SELECT concat('SBX-', lpad(p.ogc_fid::text, greatest(4,length(p.ogc_fid::text)), '0')) AS person_id,
       {", ".join(expressions)}, p.latitude, p.longitude,
       CASE WHEN p.age IS NULL THEN 'Not recorded' WHEN p.age < 18 THEN 'Under 18'
            WHEN p.age < 30 THEN '18–29' WHEN p.age < 45 THEN '30–44'
            WHEN p.age < 60 THEN '45–59' ELSE '60+' END AS age_band,
       f.facility_name, f.lat AS facility_latitude, f.lng AS facility_longitude,
       CASE WHEN p.latitude BETWEEN -90 AND 90 AND p.longitude BETWEEN -180 AND 180
                 AND f.lat BETWEEN -90 AND 90 AND f.lng BETWEEN -180 AND 180
            THEN ST_Distance(ST_SetSRID(ST_MakePoint(p.longitude,p.latitude),4326)::geography,
                             ST_SetSRID(ST_MakePoint(f.lng,f.lat),4326)::geography) / 1000.0
       END AS distance_km
FROM public.sandbox_join_2_sandbox_join_2 p
JOIN LATERAL (SELECT * FROM facilities locations
    ORDER BY CASE WHEN p.latitude BETWEEN -90 AND 90 AND p.longitude BETWEEN -180 AND 180
        THEN ST_Distance(ST_SetSRID(ST_MakePoint(p.longitude,p.latitude),4326)::geography,
                         ST_SetSRID(ST_MakePoint(locations.lng,locations.lat),4326)::geography)
        END NULLS LAST, locations.facility_name
    LIMIT 1) f ON TRUE"""
    population_id = dataset("population", population_sql)
    manifest["fields"] = fields
    manifest["population_sql"] = population_sql
    manifest["dataset_id"] = population_id
    save()

    # Capture and remove each native filter before counting stage-specific cohorts.
    # The IN values use Superset's SQL escaping filter; ranges accept only floats.
    capture = "\n".join(
        "{% set f_" + field + " = get_filters('" + field + "', remove_filter=True) %}"
        for field in ["facility_name", *fields]
    )

    def predicates(max_stage):
        output = []
        for field in ["facility_name", *[k for k, v in fields.items() if v["stage"] <= max_stage]]:
            output.append(
                "{% for filter in f_" + field + " %}\n"
                "{% if filter.get('op') == 'IN' %} AND \""
                + field
                + "\" IN {{ filter.get('val') | where_in }}\n"
                "{% elif filter.get('op') in ['>=','>','<=','<'] %} AND \""
                + field
                + "\" {{ filter.get('op') }} {{ filter.get('val') | float }}\n"
                "{% endif %}{% endfor %}"
            )
        return "\n".join(output)

    nulls = ", ".join(
        f'NULL::{"double precision" if v["kind"] == "number" else "text"} AS "{k}"'
        for k, v in fields.items()
    )
    funnel_sql = (
        capture
        + "\nWITH population AS (SELECT * FROM ("
        + population_sql
        + ") selected_facility WHERE TRUE\n"
        + predicates(0)
        + ")\n"
        + "\nUNION ALL\n".join(
            f"SELECT '{index + 1:02d} · {label.replace(chr(39), chr(39) * 2)}'::text AS stage, "
            f"(SELECT COUNT(DISTINCT person_id) FROM population WHERE TRUE\n{predicates(index)}) AS matches, "
            f"NULL::text AS facility_name, {nulls}"
            for index, label in enumerate(definitions["stages"])
        )
    )
    funnel_id = dataset("funnel", funnel_sql)
    manifest["funnel_sql"] = funnel_sql
    breakdown_fields = [
        "age_band",
        "sex",
        "blood_group",
        "blood_donor",
        "subcounty",
        "parish",
        "village",
        "hiv_status",
        "s_hbsag",
        "s_hcv",
        "s_syphillis",
        "bp_results",
        "sickle_cells_disease",
    ]
    breakdown_fields += [
        f for f, v in fields.items() if v["kind"] == "category" and f not in breakdown_fields
    ]
    summary_sql = (
        capture
        + "\nWITH population AS (SELECT * FROM ("
        + population_sql
        + ") selected_facility WHERE TRUE\n"
        + predicates(0)
        + "),\nmatched AS (SELECT * FROM population WHERE TRUE\n"
        + predicates(5)
        + ")\n"
    )
    panels = [
        f"SELECT 'stage'::text AS panel, '{index + 1:02d} · {label}'::text AS label, (SELECT COUNT(DISTINCT person_id) FROM population WHERE TRUE\n{predicates(index)}) AS people"
        for index, label in enumerate(definitions["stages"])
    ]
    panels += [
        f'SELECT \'{field}\'::text AS panel, "{field}"::text AS label, COUNT(DISTINCT person_id) AS people FROM matched GROUP BY "{field}"'
        for field in breakdown_fields
    ]
    summary_sql += "\nUNION ALL\n".join(panels)
    summary_id = dataset("summary", summary_sql)
    manifest["summary_sql"] = summary_sql
    save()

    count = {
        "expressionType": "SQL",
        "sqlExpression": "COUNT(DISTINCT person_id)",
        "label": "People",
    }
    stage_metric = {
        "expressionType": "SQL",
        "sqlExpression": "SUM(matches)",
        "label": "Matching people",
    }
    all_columns = [
        "person_id",
        *fields,
        "latitude",
        "longitude",
        "age_band",
        "facility_name",
        "facility_latitude",
        "facility_longitude",
    ]

    def chart(
        key,
        title,
        viz,
        dataset_id,
        columns=None,
        metric=count,
        params=None,
        raw=False,
        filters=None,
    ):
        form = {
            "datasource": f"{dataset_id}__table",
            "viz_type": viz,
            "adhoc_filters": [],
            "row_limit": 10000,
            "color_scheme": "supersetColors",
            "emit_filter": True,
            "number_format": ",d",
            "show_legend": True,
            "show_value": True,
            **(params or {}),
        }
        if raw:
            form.update({"query_mode": "raw", "all_columns": columns, "order_by_cols": []})
            metrics = []
        else:
            form.update({"metrics": [metric], "metric": metric})
            metrics = [metric]
            if columns:
                form.setdefault("groupby", columns)
                if viz.startswith("echarts_timeseries"):
                    form.update(
                        {
                            "x_axis": columns[0],
                            "time_grain_sqla": None,
                            "is_timeseries": False,
                            "orientation": "horizontal",
                            "x_axis_sort": columns[0],
                        }
                    )
        if filters:
            form["adhoc_filters"] = [
                {
                    "expressionType": "SIMPLE",
                    "subject": f["col"],
                    "operator": f["op"],
                    "comparator": f["val"],
                    "clause": "WHERE",
                }
                for f in filters
            ]
        context = {
            "datasource": {"id": dataset_id, "type": "table"},
            "queries": [
                {
                    "columns": columns or [],
                    "metrics": metrics,
                    "filters": filters or [],
                    "row_limit": 10000,
                }
            ],
            "result_format": "json",
            "result_type": "full",
            "form_data": form,
        }
        body = {
            "slice_name": title,
            "viz_type": viz,
            "datasource_id": dataset_id,
            "datasource_type": "table",
            "params": json.dumps(form),
            "query_context": json.dumps(context),
        }
        if key in manifest["charts"]:
            identifier = manifest["charts"][key]
            api("PUT", f"chart/{identifier}", body)
        else:
            identifier = api("POST", "chart/", body)["id"]
            manifest["charts"][key] = identifier
            save()
        return identifier

    chart(
        "summary",
        "Population discovery · Superset cohort summaries",
        "table",
        summary_id,
        ["panel", "label", "people"],
        raw=True,
    )
    if args.data_only:
        api(
            "PUT",
            f"chart/{manifest['charts']['summary']}",
            {"dashboards": [manifest["dashboards"]["discovery"]]},
        )
        save()
        print("Updated discovery datasets and summary query.")
        return

    chart(
        "current",
        "Current matching population",
        "big_number_total",
        population_id,
        params={"subheader": "Distinct Sandbox records · selected discovery criteria"},
    )
    chart(
        "funnel",
        "Population discovery · sequential criteria",
        "funnel",
        funnel_id,
        ["stage"],
        stage_metric,
        {
            "sort_by_metric": False,
            "label_type": "key_value",
            "show_labels": True,
            "number_format": ",d",
        },
    )
    for field, title in [
        ("age_band", "Age distribution"),
        ("sex", "Sex"),
        ("blood_group", "Blood group"),
        ("blood_donor", "Donor willingness"),
        ("parish", "Parish"),
        ("village", "Village"),
        ("hiv_status", "HIV screening"),
        ("s_hbsag", "Hepatitis B screening"),
        ("s_hcv", "Hepatitis C screening"),
        ("s_syphillis", "Syphilis screening"),
        ("bp_results", "Blood pressure category"),
        ("sickle_cells_disease", "Recorded sickle cell disease"),
    ]:
        chart(
            field,
            title,
            "pie" if field in ("sex", "blood_donor") else "echarts_timeseries_bar",
            population_id,
            [field],
            params={"donut": True, "label_type": "key_value", "show_labels": True},
        )
    chart(
        "source",
        "Population discovery · permitted source records",
        "table",
        population_id,
        all_columns,
        raw=True,
        filters=[],
    )
    permitted = [
        "person_id",
        "age",
        "sex",
        "blood_group",
        "blood_donor",
        "subcounty",
        "parish",
        "village",
        "distance_km",
        "hiv_status",
        "s_hbsag",
        "s_hcv",
        "s_syphillis",
        "hgb",
    ]
    chart(
        "list",
        "Matching population · authorized records",
        "table",
        population_id,
        permitted,
        raw=True,
    )
    chart(
        "facilities",
        "Discovery reference · facilities",
        "table",
        facility_id,
        ["facility_name", "lat", "lng", "subcounty", "facility_level", "beds"],
        raw=True,
    )
    chart(
        "boundaries",
        "Discovery reference · area polygons",
        "table",
        24,
        ["district", "county", "subcounty", "parish", "geometry_geojson"],
        raw=True,
    )
    chart(
        "points",
        "Matching population · point map",
        "deck_scatter",
        population_id,
        ["person_id", "latitude", "longitude", "blood_group", "subcounty"],
        params={
            "spatial": {"type": "latlong", "lonCol": "longitude", "latCol": "latitude"},
            "entity": "person_id",
            "dimension": "blood_group",
            "point_radius_fixed": {"type": "fix", "value": 80},
            "point_unit": "meters",
            "mapbox_style": "https://sandbox.lifehealth.app/lifedata-ai/static/dashboards/satellite-style.json",
            "viewport": {
                "latitude": 0.22,
                "longitude": 33.02,
                "zoom": 10,
                "bearing": 0,
                "pitch": 0,
            },
            "global_opacity": 0.8,
            "min_radius": 4,
            "max_radius": 10,
        },
        raw=True,
    )
    chart(
        "facility_points",
        "All selected facilities · locations",
        "deck_scatter",
        facility_id,
        ["facility_name", "lat", "lng"],
        raw=True,
        params={
            "spatial": {"type": "latlong", "lonCol": "lng", "latCol": "lat"},
            "entity": "facility_name",
            "point_radius_fixed": {"type": "fix", "value": 150},
            "point_unit": "meters",
            "min_radius": 6,
            "global_opacity": 1,
            "viewport": {
                "latitude": 0.22,
                "longitude": 33.02,
                "zoom": 10,
                "bearing": 0,
                "pitch": 0,
            },
        },
    )
    polygon_id = dataset(
        "polygon_parts",
        "SELECT b.subcounty, (dumped).path::text AS part, ST_AsGeoJSON((dumped).geom) AS geometry_geojson FROM public.buikwe_subcounties b CROSS JOIN LATERAL ST_Dump(b.geometry) AS dumped",
    )
    # Native area layer uses the existing real boundary dataset. Its area names
    # participate in the same native filters and click-to-filter behavior.
    chart(
        "polygons",
        "Buikwe · administrative polygons",
        "deck_polygon",
        polygon_id,
        ["subcounty", "geometry_geojson"],
        metric={"expressionType": "SQL", "sqlExpression": "COUNT(*)", "label": "Areas"},
        params={
            "line_column": "geometry_geojson",
            "line_type": "json",
            "filled": True,
            "stroked": True,
            "extruded": False,
            "global_opacity": 0.15,
            "line_width": 2,
            "line_width_unit": "pixels",
            "js_columns": ["subcounty"],
            "mapbox_style": "https://sandbox.lifehealth.app/lifedata-ai/static/dashboards/satellite-style.json",
            "viewport": {
                "latitude": 0.22,
                "longitude": 33.02,
                "zoom": 10,
                "bearing": 0,
                "pitch": 0,
            },
            "linear_color_scheme": "superset_seq_1",
            "multiplier": 1,
        },
    )
    chart(
        "map",
        "Matching population · points and area boundaries",
        "deck_multi",
        population_id,
        params={
            "deck_slices": [
                manifest["charts"]["polygons"],
                manifest["charts"]["points"],
                manifest["charts"]["facility_points"],
            ],
            "mapbox_style": "https://sandbox.lifehealth.app/lifedata-ai/static/dashboards/satellite-style.json",
            "viewport": {
                "latitude": 0.22,
                "longitude": 33.02,
                "zoom": 10,
                "bearing": 0,
                "pitch": 0,
            },
        },
    )

    sandbox_panels = json.loads((ROOT / "lifedata_ai/population_panels.json").read_text())
    for panel in sandbox_panels:
        for field, _label in panel["tabs"]:
            if field in fields:
                chart(
                    "question_" + field,
                    fields[field]["label"],
                    "pie",
                    population_id,
                    [field],
                    params={
                        "donut": True,
                        "innerRadius": 60,
                        "label_type": "key_percent",
                        "labels_outside": True,
                        "show_legend": False,
                        "label_font_size": 9,
                    },
                )
    chart(
        "question_people",
        "Number of People",
        "gauge_chart",
        population_id,
        params={
            "min_val": 0,
            "max_val": 299,
            "start_angle": 180,
            "end_angle": 0,
            "show_pointer": False,
            "show_progress": True,
            "font_size": 26,
        },
    )
    chart(
        "question_sex",
        "Sex",
        "pie",
        population_id,
        ["sex"],
        params={"donut": True, "show_legend": False, "label_type": "key_percent"},
    )
    chart(
        "question_people_mental_health",
        "Mental health",
        "pie",
        population_id,
        ["mental_health"],
        params={"donut": True, "show_legend": False, "label_type": "key_percent"},
    )
    pages = {
        "overview": (
            "Sandbox overview",
            [
                ["current", "blood_donor", "sex"],
                ["map", "blood_group"],
                ["age_band", "parish", "village"],
            ],
        ),
        "discovery": (
            "Population discovery",
            [["current", "funnel"], ["map", "list"], ["blood_group", "age_band", "sex"]],
        ),
        "profile": (
            "Matching population profile",
            [
                ["current"],
                ["map", "age_band"],
                ["sex", "blood_group", "parish"],
                ["hiv_status", "s_hbsag", "s_hcv", "bp_results"],
            ],
        ),
        "list": ("Authorized list & detail", [["current"], ["list"]]),
    }
    for key, (title, rows) in pages.items():
        position = {
            "DASHBOARD_VERSION_KEY": "v2",
            "ROOT_ID": {"id": "ROOT_ID", "type": "ROOT", "children": ["GRID_ID"]},
            "GRID_ID": {"id": "GRID_ID", "type": "GRID", "children": [], "parents": ["ROOT_ID"]},
            "HEADER_ID": {"id": "HEADER_ID", "type": "HEADER", "meta": {"text": PREFIX + title}},
        }
        intro_id = "MARKDOWN-intro"
        position[intro_id] = {
            "id": intro_id,
            "type": "MARKDOWN",
            "children": [],
            "parents": ["ROOT_ID", "GRID_ID"],
            "meta": {
                "width": 12,
                "height": 8 if key == "discovery" else 22,
                "code": "**LifeHealth Population Discovery** · Blood donation demonstration. "
                "Apply willingness → blood group → recorded screening → age/clinical → geography. "
                "**Matches selected criteria; not medical approval.** Donor willingness is not separate contact consent. "
                "Distances are straight-line to the nearest selected facility; all facilities are selected by default. Age reflects the recorded survey snapshot.",
            },
        }
        position["GRID_ID"]["children"].append(intro_id)
        for row_index, keys in enumerate(rows):
            rowid = f"ROW-{row_index}"
            position["GRID_ID"]["children"].append(rowid)
            position[rowid] = {
                "id": rowid,
                "type": "ROW",
                "children": [],
                "parents": ["ROOT_ID", "GRID_ID"],
                "meta": {"background": "BACKGROUND_TRANSPARENT"},
            }
            for chart_key in keys:
                chartid = manifest["charts"][chart_key]
                node = f"CHART-{chartid}"
                position[rowid]["children"].append(node)
                position[node] = {
                    "id": node,
                    "type": "CHART",
                    "children": [],
                    "parents": ["ROOT_ID", "GRID_ID", rowid],
                    "meta": {
                        "chartId": chartid,
                        "sliceName": chart_key,
                        "width": 12 // len(keys),
                        "height": (18 if row_index == 0 else 60 if row_index == 1 else 22)
                        if key == "discovery"
                        else 70
                        if chart_key in ("map", "funnel")
                        else 45,
                    },
                }
        members = {manifest["charts"][c] for row in rows for c in row}
        # Sources are attached to the new discovery dashboard for publishing,
        # but kept out of its presentation layout.
        if key == "discovery":
            members.update(
                manifest["charts"][c]
                for c in (
                    "source",
                    "summary",
                    "boundaries",
                    "facilities",
                    "points",
                    "polygons",
                    "facility_points",
                )
            )
        if key == "discovery":
            # Five columns exactly follow the original Sandbox's panel grouping.
            position["GRID_ID"]["children"] = ["ROW-sandbox"]
            position["ROW-sandbox"] = {
                "id": "ROW-sandbox",
                "type": "ROW",
                "parents": ["ROOT_ID", "GRID_ID"],
                "children": [],
                "meta": {"background": "BACKGROUND_TRANSPARENT"},
            }
            panel_map = {p["id"]: p for p in sandbox_panels}
            columns = [
                (2, ["people", "donor", "screening"]),
                (2, ["blood", "liver", "pressure"]),
                (4, []),
                (2, ["vaccines", "disability", "renal"]),
                (2, ["hematology", "sexual", "systemic"]),
            ]

            def add_chart(
                parent, chart_key, height, width, parents, position=position, members=members
            ):
                chart_id = manifest["charts"][chart_key]
                node = "CHART-" + str(chart_id)
                position[parent]["children"].append(node)
                position[node] = {
                    "id": node,
                    "type": "CHART",
                    "parents": parents + [parent],
                    "children": [],
                    "meta": {
                        "chartId": chart_id,
                        "sliceName": chart_key,
                        "height": height,
                        "width": width,
                    },
                }
                members.add(chart_id)

            for i, (width, panel_ids) in enumerate(columns):
                column = f"COLUMN-sandbox-{i}"
                position["ROW-sandbox"]["children"].append(column)
                ancestry = ["ROOT_ID", "GRID_ID", "ROW-sandbox"]
                position[column] = {
                    "id": column,
                    "type": "COLUMN",
                    "parents": ancestry,
                    "children": [],
                    "meta": {"width": width, "background": "BACKGROUND_TRANSPARENT"},
                }
                for panel_id in panel_ids:
                    tabs_id = "TABS-" + panel_id
                    position[column]["children"].append(tabs_id)
                    position[tabs_id] = {
                        "id": tabs_id,
                        "type": "TABS",
                        "parents": ancestry + [column],
                        "children": [],
                        "meta": {},
                    }
                    definitions = (
                        [
                            ("question_people", "Pple"),
                            ("question_sex", "Sex"),
                            ("question_people_mental_health", "MH"),
                        ]
                        if panel_id == "people"
                        else [
                            ("question_" + f, label)
                            for f, label in panel_map[panel_id]["tabs"]
                            if f in fields
                        ]
                    )
                    for j, (chart_key, label) in enumerate(definitions):
                        tab = "TAB-" + panel_id + "-" + str(j)
                        position[tabs_id]["children"].append(tab)
                        position[tab] = {
                            "id": tab,
                            "type": "TAB",
                            "parents": ancestry + [column, tabs_id],
                            "children": [],
                            "meta": {"text": label, "defaultText": label},
                        }
                        add_chart(tab, chart_key, 22, width, ancestry + [column, tabs_id])
                if i == 2:
                    add_chart(column, "map", 53, width, ancestry)
                    bottom = "ROW-centre-bottom"
                    position[column]["children"].append(bottom)
                    position[bottom] = {
                        "id": bottom,
                        "type": "ROW",
                        "parents": ancestry + [column],
                        "children": [],
                        "meta": {"background": "BACKGROUND_TRANSPARENT"},
                    }
                    add_chart(bottom, "funnel", 25, 2, ancestry + [column])
                    add_chart(bottom, "list", 25, 2, ancestry + [column])
            # Eliminate detached presentation nodes; reference charts remain members only.
            reachable = {"ROOT_ID", "GRID_ID", "HEADER_ID", "DASHBOARD_VERSION_KEY"}

            def visit(node, position=position, reachable=reachable):
                reachable.add(node)
                for child in position.get(node, {}).get("children", []):
                    visit(child)

            visit("ROOT_ID")
            position = {node: value for node, value in position.items() if node in reachable}
        filters = []
        for field, label, kind in [
            ("facility_name", "Selected facility", "category"),
            *[
                (f, v["label"], v["kind"])
                for f, v in fields.items()
                if f
                in {
                    "district",
                    "county",
                    "subcounty",
                    "parish",
                    "village",
                    "age",
                    "sex",
                    "blood_donor",
                    "blood_group",
                    "hiv_status",
                    "s_hbsag",
                    "s_hcv",
                    "s_syphillis",
                    "distance_km",
                }
            ],
        ]:
            fid = "NATIVE_FILTER-discovery-" + field
            native = {
                "id": fid,
                "name": label,
                "filterType": "filter_select" if kind == "category" else "filter_range",
                "targets": [
                    {
                        "datasetId": facility_id if field == "facility_name" else population_id,
                        "column": {"name": field},
                    }
                ],
                "scope": {"rootPath": ["ROOT_ID"], "excluded": []},
                "cascadeParentIds": [],
                "controlValues": {
                    "multiSelect": True,
                    "enableEmptyFilter": False,
                    "searchAllOptions": True,
                },
                "defaultDataMask": {"extraFormData": {}, "filterState": {}},
            }
            if field == "facility_name":
                native["description"] = (
                    "All facilities by default. Distance uses the nearest selected facility."
                )
            filters.append(native)
        metadata = {
            "native_filter_configuration": filters,
            "filter_bar_orientation": "HORIZONTAL" if key == "discovery" else "VERTICAL",
            "color_scheme": "supersetColors",
            "cross_filters_enabled": True,
            "refresh_frequency": 0,
            "timed_refresh_immune_slices": sorted(members),
            "expanded_slices": {},
            "label_colors": {
                "Yes": "#08f500",
                "No": "#ff0707",
                "NEGATIVE": "#08f500",
                "Negative": "#08f500",
                "Positive": "#ff0707",
                "POSITIVE": "#ff0707",
                "Normal": "#08f500",
                "High": "#ff0707",
                "Low": "#ffb500",
            },
            "chart_configuration": {},
        }
        body = {
            "dashboard_title": PREFIX + title,
            "published": True,
            "position_json": json.dumps(position),
            "json_metadata": json.dumps(metadata),
            "owners": [credentials["user_id"]],
            **(
                {
                    "css": ".dashboard-content,.dashboard-grid,.dashboard-component-chart-holder,.dashboard-component-tabs{background:#202020!important;color:#ddd!important} .dashboard-component-chart-holder,.dashboard-component-tabs{border:1px solid #405b93!important} .dashboard-component-chart-holder{padding:4px!important} .chart-header,.chart-header a,.ant-tabs-tab,.ant-tabs-tab-btn{color:#ddd!important;font-size:10px!important} .ant-tabs-nav{margin-bottom:0!important} .dashboard-component-tabs{display:flex;flex-direction:column} .dashboard-component-tabs>.ant-tabs>.ant-tabs-nav{order:2} .dashboard-component-tabs>.ant-tabs{display:flex;flex-direction:column} .chart-container{background:#202020!important} .filter-bar{background:#202020!important}"
                }
                if key == "discovery"
                else {}
            ),
        }
        if key in manifest["dashboards"]:
            identifier = manifest["dashboards"][key]
            api("PUT", f"dashboard/{identifier}", body)
        else:
            identifier = api("POST", "dashboard/", body)["id"]
            manifest["dashboards"][key] = identifier
            save()
        # Chart dashboard membership is set using the documented chart update API.
        for chart_id in members:
            existing = api("GET", f"chart/{chart_id}")["result"]
            dashboards = {
                d["id"] if isinstance(d, dict) else d for d in existing.get("dashboards", [])
            }
            api("PUT", f"chart/{chart_id}", {"dashboards": sorted(dashboards | {identifier})})
        print("Created/updated NEW dashboard", identifier, title)
    save()
    print("Manifest:", args.manifest)


if __name__ == "__main__":
    main()
