"""Permission-checked geographic sources for the shared custom workspace."""
import json
import math
import re
from collections import defaultdict
from lifedata_ai.tools.base import ToolError
from lifedata_ai.tools.superset_read import _parse_json_field, _prepare_query_context

FACILITY_COLUMNS = ('facility_name', 'name_facility', 'name_of_he', 'name', 'lat', 'latitude', 'lng', 'lon', 'longitude',
                    'facility_level', 'hf_level', 'level_of_h', 'level', 'beds', 'no_beds', 'how_many_b', 'x', 'y', 'district', 'county', 'subcounty', 'ward', 'parish', 'village')
BOUNDARY_COLUMNS = ('district', 'county', 'subcounty', 'geometry_geojson', 'geometry', 'geojson')


def name_key(value):
    return re.sub(r'[^a-z0-9]', '', str(value or '').lower())


def first(row, *names):
    return next((row[n] for n in names if row.get(n) not in (None, '')), None)


def number(value):
    if value is None or str(value).strip() == '':
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def source_rows(client, chart_id, members, columns, require_all=False):
    if type(chart_id) is not int or chart_id not in members:
        raise ToolError('Geographic sources must belong to this dashboard.', code='forbidden')
    chart = client.get_chart(chart_id)
    context, note = _parse_json_field(chart.get('query_context'), 'query_context')
    prepared = _prepare_query_context(context, chart, note, row_limit_cap=10000)
    if len(prepared['queries']) != 1 or prepared['queries'][0].get('metrics') or prepared['queries'][0].get('post_processing'):
        raise ToolError('Geographic reference charts must contain raw records.', code='invalid_source')
    dataset = client.get_dataset(prepared['datasource']['id'])
    available = {c['column_name'] for c in dataset.get('columns', [])}
    if require_all and not set(columns).issubset(available):
        raise ToolError('Administrative source fields must exist in its permitted dataset.', code='invalid_source')
    projection = list(dict.fromkeys(c for c in columns if c in available))
    if not projection:
        raise ToolError('The geographic source has no usable map fields.', code='invalid_source')
    query = prepared['queries'][0]
    query['columns'] = projection
    # Keep the saved source filters and existing result cap; never read another dataset.
    query['row_limit'] = min(query['row_limit'], 10000)
    result = (client.chart_data(prepared).get('result') or [{}])[0]
    if result.get('error'):
        raise ToolError('The geographic source query failed.', code='query_failed')
    rows = result.get('data') or []
    return rows, len(rows) >= query['row_limit']


def workspace_geography(client, config, members, selected_rows, criteria):
    settings = config['workspace']
    facilities, boundaries, notes = [], [], []
    truncated = False
    if config.get('facility_chart_id'):
        raw, capped = source_rows(client, config['facility_chart_id'], members, FACILITY_COLUMNS)
        truncated |= capped
        seen, invalid_points = set(), 0
        for row in raw:
            name = first(row, 'facility_name', 'name_facility', 'name_of_he', 'name')
            lat = number(first(row, 'lat', 'latitude', 'y'))
            lng = number(first(row, 'lng', 'lon', 'longitude', 'x'))
            if not name or lat is None or lng is None or abs(lat) > 90 or abs(lng) > 180:
                invalid_points += 1
                continue
            key = (name_key(name), lat, lng)
            if key in seen:
                continue
            seen.add(key)
            beds = number(first(row, 'beds', 'no_beds', 'how_many_b'))
            facilities.append({'facility_name': str(name), 'lat': lat, 'lng': lng,
                'facility_level': first(row, 'facility_level', 'hf_level', 'level_of_h', 'level'),
                'beds': beds if beds is not None and beds >= 0 else None,
                **{k: row.get(k) for k in ('district', 'county', 'subcounty')}})
        if invalid_points:
            notes.append(f'{invalid_points} geographic records have missing or invalid coordinates.')
    if config.get('boundary_chart_id'):
        from django.contrib.gis.geos import GEOSGeometry
        from django.contrib.gis.geos.error import GEOSException
        raw, capped = source_rows(client, config['boundary_chart_id'], members, BOUNDARY_COLUMNS)
        truncated |= capped
        grouped = defaultdict(list)
        invalid = 0
        for row in raw:
            try:
                geometry = first(row, 'geometry_geojson', 'geometry', 'geojson')
                geometry = json.loads(geometry) if isinstance(geometry, str) else geometry
                if geometry.get('type') == 'Feature':
                    geometry = geometry['geometry']
                part = GEOSGeometry(json.dumps(geometry), srid=4326)
                if part.geom_type not in ('Polygon', 'MultiPolygon') or part.empty or not part.valid:
                    raise ValueError('Invalid polygon')
                district, county, subcounty = (str(row.get(k) or '') for k in ('district', 'county', 'subcounty'))
                if subcounty:
                    grouped[('subcounty', district, county, subcounty)].append(part)
                if county:
                    grouped[('county', district, county, '')].append(part)
            except (ValueError, TypeError, AttributeError, KeyError, GEOSException):
                invalid += 1
        for (level, district, county, subcounty), parts in sorted(grouped.items()):
            geometry = parts[0]
            for part in parts[1:]:
                geometry = geometry.union(part)
            boundaries.append({'area_level': level, 'area_name': subcounty if level == 'subcounty' else county,
                'district': district, 'county': county, 'subcounty': subcounty,
                'field': settings.get(level + '_field', level), 'geometry_geojson': json.loads(geometry.geojson)})
        if invalid:
            notes.append(f'{invalid} boundary records have missing or invalid polygons.')
    site_field = settings['site_field']
    selected_names = {name_key(r.get(site_field)) for r in selected_rows if r.get(site_field)} if site_field else set()
    visible = [f for f in facilities if not site_field or name_key(f['facility_name']) in selected_names]
    for level in ('county', 'subcounty'):
        field = settings.get(level + '_field', level)
        chosen = {name_key(v) for v in criteria.get(field, [])}
        if chosen:
            visible = [f for f in visible if name_key(f.get(level)) in chosen]
    matched_names = {name_key(f['facility_name']) for f in facilities}
    unmatched = len(selected_names - matched_names)
    if unmatched and config.get('facility_chart_id'):
        notes.append(f'{unmatched} selected facility names have no matching location in the reference source.')
    if truncated:
        notes.append('A geographic source reached its saved row limit; map coverage may be incomplete.')
    return {'facilities': visible, 'all_facilities': facilities, 'boundaries': boundaries,
            'counts': {level: sum(b['area_level'] == level for b in boundaries) for level in ('county', 'subcounty')},
            'truncated': truncated, 'notes': notes, 'unmatched_facilities': unmatched,
            'source_chart_ids': [config[k] for k in ('facility_chart_id', 'boundary_chart_id') if config.get(k)]}
