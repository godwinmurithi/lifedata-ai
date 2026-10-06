"""Discover administrative boundaries across the caller's permitted data sources."""
from lifedata_ai.tools.base import Tool, ToolMode
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
        'properties': {'region': {'type': 'string', 'minLength': 1, 'maxLength': 100}}, 'required': ['region']}

    def run(self, context, region):
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
        return {'region': region, 'candidates': candidates, 'unavailable_sources': denied,
            'message': 'Verify country coverage and ordered parent fields before using a reference; names alone do not establish a match.'}
