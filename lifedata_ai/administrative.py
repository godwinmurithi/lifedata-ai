"""Join permitted administrative polygons to records without inventing locations."""
import json
from collections import defaultdict
from functools import lru_cache
from django.contrib.gis.geos import GEOSGeometry, GeometryCollection, Point
from django.contrib.gis.geos.error import GEOSException
from lifedata_ai.geography import first, name_key, number, source_rows

LEVEL_SCHEMA = {'type': 'object', 'additionalProperties': False, 'properties': {
    'field': {'type': 'string', 'pattern': '^[A-Za-z_][A-Za-z0-9_]*$'},
    'source_field': {'type': 'string', 'minLength': 1},
    'label': {'type': 'string', 'minLength': 1, 'maxLength': 40}},
    'required': ['field', 'source_field', 'label']}
ADMINISTRATIVE_SCHEMA = {'type': 'array', 'minItems': 1, 'maxItems': 8, 'items': {
    'type': 'object', 'additionalProperties': False, 'properties': {
        'chart_id': {'type': 'integer', 'minimum': 1},
        'geometry_field': {'type': 'string', 'minLength': 1},
        'levels': {'type': 'array', 'minItems': 1, 'maxItems': 8, 'items': LEVEL_SCHEMA}},
    'required': ['chart_id', 'geometry_field', 'levels']}}
CATALOGUE_SCHEMA = {'type':'array', 'minItems':1, 'maxItems':300, 'items':{
    'type':'object','additionalProperties':False,'properties':{
        'release_id':{'type':'string','maxLength':100},
        'field':{'type':'string','pattern':'^[A-Za-z_][A-Za-z0-9_]*$'},
        'label':{'type':'string','minLength':1,'maxLength':120}},
    'required':['release_id','field','label']}}


@lru_cache(maxsize=128)
def dissolved_shape(parts):
    # Only geometry operations are cached. Every request still runs the
    # reference query with its own viewer permissions and saved filters; the
    # cache key contains the exact authorised geometries returned this time.
    geometries = [GEOSGeometry(memoryview(part), srid=4326) for part in parts]
    return geometries[0] if len(geometries) == 1 else GeometryCollection(*geometries).unary_union


def dissolved_geojson(parts):
    return dissolved_shape(parts).geojson


def dissolved_marker_point(parts):
    # `centroid` is the mean of all vertices and routinely lands outside a
    # concave or multi-part shape (a bay, a strait, an exclave) — exactly
    # where a "click this country" marker must not be. `point_on_surface`
    # is GEOS's guaranteed-interior point: always on the shape itself.
    point = dissolved_shape(parts).point_on_surface
    return {'lat': point.y, 'lng': point.x}


def hierarchy(config):
    result = []
    for source in config.get('boundary_catalogue', []):
        if source['field'] not in {item['field'] for item in result}:
            result.append({'field':source['field'],'label':source['label']})
    for source in config.get('administrative_boundaries', []):
        for level in source['levels']:
            if level['field'] not in {item['field'] for item in result}:
                result.append({'field': level['field'], 'label': level['label']})
    return result


def area_name(value):
    value = str(value or '').strip()
    return None if name_key(value) in {'', 'noentry', 'unknown', 'notrecorded', 'null', 'na'} else value


def enrich_administrative(client, config, members, rows, geography):
    """Spatial joins use real coordinates; ambiguous or missing matches stay missing."""
    levels = hierarchy(config)
    if not levels:
        return geography, levels
    geo = geography or {'facilities': [], 'all_facilities': [], 'boundaries': [], 'counts': {},
                       'notes': [], 'truncated': False, 'source_chart_ids': []}
    geo['hierarchy'] = levels
    pieces = []
    invalid = 0
    if config.get('boundary_catalogue'):
        from lifedata_ai.models import BoundaryRelease
        from lifedata_ai.tools.base import ToolError
        for source in config['boundary_catalogue']:
            release = BoundaryRelease.objects.filter(pk=source['release_id'],status='ready').first()
            if not release:
                raise ToolError('Saved boundary catalogue release is unavailable.',code='invalid_source')
            geo['notes'].append(f'{release.country_name} · {release.label} · reference {release.represented_year} · '
                                f'{release.attribution} · {release.license} · release {release.pk}')
            for area in release.areas.all().iterator(chunk_size=100):
                geometry = GEOSGeometry(json.dumps(area.geometry_geojson),srid=4326)
                pieces.append((geometry,geometry.prepared,{source['field']:area.area_name},geometry.extent))
    for source in config.get('administrative_boundaries', []):
        columns = [l['source_field'] for l in source['levels']] + [source['geometry_field']]
        raw, capped = source_rows(client, source['chart_id'], members, columns, require_all=True)
        geo['truncated'] |= capped
        if source['chart_id'] not in geo['source_chart_ids']:
            geo['source_chart_ids'].append(source['chart_id'])
        for record in raw:
            try:
                value = record[source['geometry_field']]
                value = json.loads(value) if isinstance(value, str) else value
                if value.get('type') == 'Feature':
                    value = value['geometry']
                geometry = GEOSGeometry(json.dumps(value), srid=4326)
                if geometry.geom_type not in ('Polygon', 'MultiPolygon') or geometry.empty or not geometry.valid:
                    raise ValueError('Invalid polygon')
                names = {l['field']: area_name(record.get(l['source_field'])) for l in source['levels']}
                pieces.append((geometry, geometry.prepared, names, geometry.extent))
            except (ValueError, TypeError, AttributeError, KeyError, GEOSException):
                invalid += 1
    if invalid:
        geo['notes'].append(f'{invalid} administrative polygons are missing or invalid.')
    spatial_cache = {}
    def assign(record):
        lat = number(first(record, 'lat', 'latitude', 'y'))
        lng = number(first(record, 'lng', 'lon', 'longitude', 'x'))
        known = {l['field']: area_name(record.get(l['field'])) for l in levels}
        known = {k: v for k, v in known.items() if v}
        if lat is not None and lng is not None and abs(lat) <= 90 and abs(lng) <= 180:
            key = (lat, lng)
            if key not in spatial_cache:
                point = Point(lng, lat, srid=4326)
                spatial_cache[key] = [names for geometry, prepared, names, extent in pieces
                    if extent[0] <= lng <= extent[2]
                    and extent[1] <= lat <= extent[3] and prepared.covers(point)]
            matches = spatial_cache[key]
            # Existing source names are authoritative. Use them to disambiguate
            # shared polygon edges, but never silently overwrite conflicting names.
            compatible = [n for n in matches if all(not n.get(k) or name_key(n[k]) == name_key(v) for k, v in known.items())]
            matches = compatible
        elif known:
            matches = [n for _, _, n, _ in pieces if all(n.get(k) and name_key(n[k]) == name_key(v) for k, v in known.items())]
        else:
            matches = []
        ambiguous = False
        for level in levels:
            field = level['field']
            candidates = {n[field] for n in matches if n.get(field)}
            if len({name_key(v) for v in candidates}) > 1:
                ambiguous = True
                break
            if not area_name(record.get(field)) and len({name_key(v) for v in candidates}) == 1:
                record[field] = sorted(candidates)[0]
        return bool(matches) and not ambiguous
    unmatched = sum(not assign(facility) for facility in geo['all_facilities'])
    by_name = defaultdict(list)
    for facility in geo['all_facilities']:
        by_name[name_key(facility['facility_name'])].append(facility)
    matched_rows = 0
    for record in rows:
        candidates = by_name.get(name_key(record.get(config['workspace']['site_field'])), [])
        for level in levels:
            field = level['field']
            names = {f[field] for f in candidates if area_name(f.get(field))}
            if not area_name(record.get(field)) and len({name_key(v) for v in names}) == 1:
                record[field] = sorted(names)[0]
        matched_rows += bool(assign(record))
    geo['join_diagnostics'] = {'records_checked':len(rows), 'matched_records':matched_rows,
                               'unmatched_or_ambiguous_records':len(rows)-matched_rows}
    if unmatched:
        geo['notes'].append(f'{unmatched} facility locations could not be uniquely matched to the administrative source.')
    grouped = defaultdict(list)
    for geometry, _, names, _ in pieces:
        parents = {}
        for level in levels:
            field = level['field']; value = names.get(field)
            if value:
                key = (field, value, tuple(parents.items()))
                grouped[key].append(geometry)
                parents[field] = value
    coverage = [*geo['all_facilities'], *rows]
    coverage_indices = {}
    def covered(field, value, parents):
        keys = (field, *(key for key, _ in parents))
        if keys not in coverage_indices:
            coverage_indices[keys] = {tuple(name_key(record.get(key)) for key in keys) for record in coverage}
        return (name_key(value), *(name_key(value) for _, value in parents)) in coverage_indices[keys]
    for (field, value, parents), parts in grouped.items():
        path = dict(parents); path[field] = value
        # Render relevant coverage, including full parent polygons. The raw
        # reference can contain a whole nation without drawing thousands of
        # unrelated parish polygons on a client's map.
        if not covered(field, value, parents):
            continue
        wkb_parts = tuple(bytes(part.wkb) for part in parts)
        geo['boundaries'].append({'area_level': field, 'area_name': value, 'field': field,
            'label': next(l['label'] for l in levels if l['field'] == field), 'parents': dict(parents),
            'geometry_geojson': json.loads(dissolved_geojson(wkb_parts)),
            # Every entry here already passed `covered()` above, i.e. has at
            # least one real record — so a marker here always means "has a
            # submission", never an empty area.
            'representative_point': dissolved_marker_point(wkb_parts)})
    for level in levels:
        geo['counts'][level['field']] = sum(b['field'] == level['field'] for b in geo['boundaries'])
    if geo['truncated']:
        note = 'An administrative source reached its saved row limit; geographic matches may be incomplete.'
        if note not in geo['notes']:
            geo['notes'].append(note)
    return geo, levels


def filter_geography(geography, settings, rows, criteria, levels):
    if geography is None:
        return None
    selected = {name_key(r.get(settings['site_field'])) for r in rows if r.get(settings['site_field'])}
    geography['facilities'] = [f for f in geography['all_facilities']
        if (not settings['site_field'] or name_key(f['facility_name']) in selected)
        and all(not criteria.get(l['field']) or name_key(f.get(l['field'])) in {name_key(v) for v in criteria[l['field']]} for l in levels)]
    geography['boundaries'] = [b for b in geography['boundaries']
        if all(not criteria.get(field) or name_key(value) in {name_key(v) for v in criteria[field]}
               for field, value in b.get('parents', {}).items())]
    return geography
