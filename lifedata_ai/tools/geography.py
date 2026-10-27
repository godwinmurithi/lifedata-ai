"""Discover administrative boundaries across the caller's permitted data sources."""
from lifedata_ai.tools.base import Tool, ToolMode, ToolError
from lifedata_ai.superset_client import SupersetError


class DiscoverAdministrativeBoundariesTool(Tool):
    name = 'discover_administrative_boundaries'
    mode = ToolMode.READ
    description = (
        'Find accessible administrative boundary datasets for the CURRENT country/region, '
        'including references outside the primary survey dataset. Returns schemas and geographic '
        'samples; never assumes an admin dataset belongs to the requested country. Check samples, '
        'then reuse/create a minimal raw GeoJSON chart with parent-name fields, transforming native '
        'geometry to WGS84 if needed. Pass ordered levels to publish_dashboard administrative_boundaries. '
        'Do not create a Superset dashboard. Do not invent ward/county names or reuse benchmark geography.')
    input_schema = {'type': 'object', 'additionalProperties': False,
        'properties': {'region': {'type': 'string', 'minLength': 1, 'maxLength': 100},
                       'include_project_sources': {'type':'boolean'}}, 'required': ['region']}

    def run(self, context, region, include_project_sources=False):
        from lifedata_ai.boundary_catalogue import catalogue_layers, layer_summary
        catalogue = [layer_summary(r) for r in catalogue_layers(region)]
        if catalogue and not include_project_sources:
            return {'region':region,'catalogue':catalogue,'candidates':[],
                    'message':'Use exact-country ready release IDs in boundary_catalogue=[{release_id,field,label}], '
                              'ordered largest to smallest. Preserve returned source labels and reference years; '
                              'ADM numbers have no universal local meaning. No source chart is needed. '
                              'Set include_project_sources=true to also search permitted project-specific overrides.'}
        client = context.superset_client()
        datasets = {}
        for term in (region, 'bound', 'admin', 'district', 'county', 'ward', 'parish', 'village'):
            for row in client.list_datasets(term):
                datasets.setdefault(row['id'], row)
        candidates = []
        denied = 0
        for identifier, row in datasets.items():
            if len(candidates) >= 20:
                break
            try:
                dataset = client.get_dataset(identifier)
                columns = [c['column_name'] for c in dataset.get('columns', [])]
                geometry = [c for c in columns if any(s in c.lower() for s in ('geometry', 'geom', 'geojson'))]
                fields = [c for c in columns if any(s in c.lower() for s in ('district', 'county', 'parish', 'ward', 'village', 'region', 'country', 'adm', 'iso'))]
                if not geometry or not fields:
                    continue
                sample = client.chart_data({'datasource': {'id': identifier, 'type': 'table'},
                    'queries': [{'columns': fields[:12], 'metrics': [], 'row_limit': 2}],
                    'result_type': 'full', 'result_format': 'json'})
                result = (sample.get('result') or [{}])[0]
                candidates.append({'dataset_id': identifier, 'title': dataset.get('table_name'),
                    'database_id': (dataset.get('database') or {}).get('id'),
                    'geometry_fields': geometry, 'administrative_fields': fields,
                    'geographic_sample': result.get('data') or [], 'query_error': result.get('error')})
            except SupersetError as exc:
                if exc.status_code not in (401, 403, 404):
                    raise
                denied += 1
        return {'region': region, 'catalogue': catalogue, 'candidates': candidates, 'unavailable_sources': denied,
            'catalogue_usage': 'Prefer a ready shared catalogue release for the exact country. '
                'Pass boundary_catalogue=[{release_id, field, label}] in workspace configuration, ordered largest to smallest. '
                'Use the returned local label; ADM numbers do not have universal meanings. '
                'These public reference layers require no source chart. Saved release IDs pin geometry and provenance.',
            'message': 'Verify country coverage and ordered parent fields before using a reference; names alone do not establish a match.'}


class PrepareGeographicMapTool(Tool):
    name = 'prepare_geographic_map'
    mode = ToolMode.READ
    description = ('Dry-run administrative-name and WGS84 spatial joins against permitted saved raw charts. '
                   'Returns coverage, unmatched counts, provenance and truncation; does not publish or change charts. '
                   'Use before publishing maps; inspect unmatched records and source limits rather than invent locations.')
    from lifedata_ai.administrative import ADMINISTRATIVE_SCHEMA, CATALOGUE_SCHEMA
    input_schema = {'type': 'object', 'additionalProperties': False, 'properties': {
        'source_chart_id': {'type':'integer','minimum':1},
        'site_field': {'type':'string','minLength':1},
        'administrative_boundaries': ADMINISTRATIVE_SCHEMA,
        'boundary_catalogue': CATALOGUE_SCHEMA}, 'required':['source_chart_id','site_field']}

    def run(self, context, source_chart_id, site_field, administrative_boundaries=None, boundary_catalogue=None):
        from lifedata_ai.administrative import enrich_administrative, hierarchy, area_name
        from lifedata_ai.geography import source_rows
        if not administrative_boundaries and not boundary_catalogue:
            raise ToolError('Supply discovered administrative boundaries or catalogue releases.', code='invalid_source')
        config = {'workspace':{'site_field':site_field},
                  'administrative_boundaries':administrative_boundaries or [],
                  'boundary_catalogue':boundary_catalogue or []}
        client = context.superset_client()
        members = {source_chart_id, *(s['chart_id'] for s in config['administrative_boundaries'])}
        fields = [l['field'] for l in hierarchy(config)]
        rows, truncated = source_rows(client, source_chart_id, members,
            [site_field, *fields, 'lat','latitude','lng','lon','longitude','x','y'])
        # Return diagnostics rather than sending person-level records back to the model.
        before = {f:sum(bool(area_name(r.get(f))) for r in rows) for f in fields}
        geography, levels = enrich_administrative(client, config, members, rows, None)
        coverage = {f:{'source_named_records':before[f],
                       'named_records_after_join':sum(bool(area_name(r.get(f))) for r in rows),
                       'missing_name_records':sum(not area_name(r.get(f)) for r in rows),
                       'rendered_areas':geography['counts'].get(f,0)} for f in fields}
        complete = sum(all(area_name(r.get(f)) for f in fields) for r in rows)
        return {'records_checked':len(rows), 'join_diagnostics':geography['join_diagnostics'],
                'records_with_all_level_names':complete,
                'records_missing_level_names':len(rows)-complete, 'hierarchy':levels, 'coverage':coverage,
                'truncated':truncated or geography['truncated'], 'notes':geography['notes'],
                'join_policy':'Existing names remain authoritative; coordinates must match compatible polygons. '
                              'Ambiguous or missing matches stay unresolved. Named records alone do not prove a spatial match.',
                'next_step':'Inspect missing coverage and provenance. Truncated sources require a complete geographic query '
                            'before claiming complete coverage. This diagnostic does not publish a dashboard.'}
