"""Authenticated execution of preserved, registered dashboard templates."""
import json
from django.http import HttpResponse, JsonResponse

from lifedata_ai.models import DashboardPublication
from lifedata_ai.publication_sources import publication_charts
from lifedata_ai.superset_client import SupersetError
from lifedata_ai.tools.base import ToolError
from lifedata_ai.views.dashboards import _client, _error


def document(request, pk):
    if request.method != 'GET':
        return JsonResponse({'error': 'method_not_allowed'}, status=405)
    publication = DashboardPublication.objects.filter(pk=pk).first()
    if not publication or publication.configuration.get('preset') != 'reference-template':
        return JsonResponse({'error': 'not_found'}, status=404)
    try:
        charts = publication_charts(_client(request), publication)
        from lifedata_ai.reference_templates import build_reference_document, validate_reference_template
        config = publication.configuration
        validate_reference_template(config, {int(chart['id']) for chart in charts})
        source = build_reference_document(str(publication.id), publication.title, config['bindings'])
    except (SupersetError, ToolError) as exc:
        return _error(exc)
    response = HttpResponse(source, content_type='text/html; charset=utf-8')
    response['Cache-Control'] = 'private, no-store'
    response['X-Frame-Options'] = 'SAMEORIGIN'
    response['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    return response


def preview(request, template):
    if request.method != 'GET':
        return JsonResponse({'error': 'method_not_allowed'}, status=405)
    if template != 'sincep-hdss':
        return JsonResponse({'error': 'not_found'}, status=404)
    from lifedata_ai.reference_templates import build_reference_document
    response = HttpResponse(build_reference_document(), content_type='text/html; charset=utf-8')
    response['Cache-Control'] = 'private, no-store'
    response['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    return response


def source(request, template):
    """Download the exact supplied source; no client data is embedded."""
    if request.method != 'GET':
        return JsonResponse({'error': 'method_not_allowed'}, status=405)
    if template != 'sincep-hdss':
        return JsonResponse({'error': 'not_found'}, status=404)
    from lifedata_ai.reference_templates import MASTER, MASTER_SHA256
    from hashlib import sha256
    artifact = MASTER.read_bytes()
    if sha256(artifact).hexdigest() != MASTER_SHA256:
        return JsonResponse({'error': 'template_integrity'}, status=500)
    response = HttpResponse(artifact, content_type='application/octet-stream')
    response['Content-Disposition'] = 'attachment; filename="sincep-hdss.html"'
    response['ETag'] = '"' + MASTER_SHA256 + '"'
    return response


def map_data(request, pk, slot):
    """Join permitted raw records to named polygons for a bound template map."""
    if request.method != 'GET':
        return JsonResponse({'error': 'method_not_allowed'}, status=405)
    publication = DashboardPublication.objects.filter(pk=pk).first()
    if not publication or publication.configuration.get('preset') != 'reference-template':
        return JsonResponse({'error': 'not_found'}, status=404)
    config = publication.configuration
    binding = config.get('bindings', {}).get(slot, {})
    mapping = binding.get('geography')
    if not mapping:
        return JsonResponse({'error': 'not_found'}, status=404)
    try:
        from lifedata_ai.administrative import hierarchy, enrich_administrative
        from lifedata_ai.geography import source_rows, name_key
        from lifedata_ai.reference_templates import validate_reference_template
        client = _client(request)
        members = {int(chart['id']) for chart in publication_charts(client, publication)}
        validate_reference_template(config, members)
        join_config = {**mapping, 'workspace': {'site_field': mapping.get('site_field', '')}}
        fields = [level['field'] for level in hierarchy(join_config)]
        criteria = json.loads(request.GET.get('criteria', '{}'))
        records, truncated = source_rows(client, mapping['source_chart_id'], members,
            [mapping.get('site_field', ''), *fields, 'lat', 'latitude', 'lng', 'lon', 'longitude', 'x', 'y'],
            configuration=config, criteria=criteria)
        geography, _ = enrich_administrative(client, join_config, members, records, None)
        result = []
        for area in geography['boundaries']:
            path = {**area.get('parents', {}), area['field']: area['area_name']}
            count = sum(all(name_key(row.get(field)) == name_key(value) for field, value in path.items())
                        for row in records)
            result.append({'geometry_geojson': area['geometry_geojson'], 'area_name': area['area_name'],
                           'area_level': area['field'], 'record_count': count, **path})
        columns = list(dict.fromkeys(['geometry_geojson', 'area_name', 'area_level', 'record_count', *fields]))
        return JsonResponse({'rows': result, 'columns': columns, 'form_data': {'viz_type': 'map'},
                             'row_limit': 10000, 'truncated': truncated or geography['truncated'],
                             'join_diagnostics': geography.get('join_diagnostics', {}),
                             'notes': geography['notes'], 'location_basis': 'matched_administrative_polygons'})
    except (SupersetError, ToolError) as exc:
        return _error(exc)
    except (ValueError, TypeError, KeyError):
        return JsonResponse({'error': 'Invalid map binding or criteria.'}, status=400)
