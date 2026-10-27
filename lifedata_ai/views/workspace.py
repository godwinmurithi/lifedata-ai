"""Client data adapter for the shared Buikwe workspace; no benchmark chart dependency."""
import copy
import json
import hashlib
from collections import Counter

from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from lifedata_ai.models import DashboardPublication
from lifedata_ai.publication_sources import publication_charts
from lifedata_ai.administrative import hierarchy, enrich_administrative, filter_geography
from lifedata_ai.geography import workspace_geography, name_key
from lifedata_ai.tools.base import ToolError
from lifedata_ai.tools.superset_read import _parse_json_field, _prepare_query_context
from lifedata_ai.superset_client import SupersetError
from lifedata_ai.views.dashboards import _client, _error


def values(value):
    if value is None or value == '':
        return [None]
    if isinstance(value, str) and value.strip().startswith('['):
        try:
            decoded = json.loads(value)
            if isinstance(decoded, list):
                value = decoded
        except ValueError:
            pass
    if isinstance(value, list):
        return list(dict.fromkeys(str(v) for v in value if v is not None)) or [None]
    return [str(value)]


@csrf_exempt
def data(request, pk):
    if request.method not in ('GET', 'POST'):
        return JsonResponse({'error': 'method_not_allowed'}, status=405)
    publication = DashboardPublication.objects.filter(pk=pk).first()
    if not publication or publication.configuration.get('preset') != 'buikwe-workspace':
        return JsonResponse({'error': 'not_found'}, status=404)
    try:
        client = _client(request)
        charts = publication_charts(client, publication)
        config = publication.configuration
        if config['source_chart_id'] not in {c['id'] for c in charts}:
            return JsonResponse({'error': 'forbidden'}, status=403)
        chart = client.get_chart(config['source_chart_id'])
        context, note = _parse_json_field(chart.get('query_context'), 'query_context')
        prepared = _prepare_query_context(context, chart, note, row_limit_cap=10000)
        if len(prepared['queries']) != 1 or prepared['queries'][0].get('metrics') or prepared['queries'][0].get('post_processing'):
            raise ToolError('The workspace requires a raw record source chart.', code='invalid_source')
        settings = config['workspace']
        levels = hierarchy(config)
        administrative_fields = [l['field'] for l in levels]
        fields = list(filter(None, dict.fromkeys([*administrative_fields, *settings['selectors'], settings['site_field'], settings['country_field'],
            *settings['record_columns'], *[t['field'] for p in settings['panels'] for t in p['tabs']],
            *[step['field'] for step in settings['workflow']],
            *[t.get('measure') for p in settings['panels'] for t in p['tabs']]])))
        dataset = client.get_dataset(prepared['datasource']['id'])
        available = {c['column_name'] for c in dataset.get('columns', [])}
        # Real coordinates must reach the join even when the user did not put
        # them in the detail table or choose a separate facility reference.
        if levels:
            fields += [f for f in ('lat', 'latitude', 'lng', 'lon', 'longitude', 'x', 'y')
                       if f in available and f not in fields]
        if not config.get('facility_chart_id') and config.get('map_style'):
            from lifedata_ai.map_style import map_fields
            fields += [f for f in map_fields(config['map_style']) if f in available and f not in fields]
        if not (set(fields) - set(administrative_fields)).issubset(available):
            raise ToolError('Workspace fields must exist in the current client dataset.', code='invalid_source')
        query = copy.deepcopy(prepared['queries'][0]);query['columns'] = [f for f in fields if f in available];query['metrics'] = [];query['row_limit'] = 10000
        prepared['queries'] = [query]
        body = json.loads(request.body or '{}') if request.method == 'POST' else {}
        criteria = body.get('criteria', {})
        if not isinstance(criteria, dict) or set(criteria) - set(fields) or any(not isinstance(v,list) or any(not isinstance(x,str) for x in v) for v in criteria.values()):
            raise ToolError('Invalid workspace selection.', code='invalid_criteria')
        # Permissions and source metadata above are checked on every request.
        # Only the expensive unfiltered source/join is reused, within this
        # viewer's session, so RLS results never cross session boundaries.
        stamp = json.dumps([str(publication.pk), publication.updated_at.isoformat(),
                            config, prepared, getattr(request, 'superset_session_cookie', ''),
                            request.superset_identity.username], sort_keys=True)
        key = 'workspace:source:' + hashlib.sha256(stamp.encode()).hexdigest()
        snapshot = None if body.get('refresh') else cache.get(key)
        if snapshot is None:
            payload = client.chart_data(prepared)
            result = (payload.get('result') or [{}])[0]
            if result.get('error'):
                raise ToolError('Superset could not run the workspace source.', code='query_failed')
            rows = result.get('data') or []
            members = {int(c['id']) for c in charts}
            geography = workspace_geography(client, config, members, rows, {}) if config.get('facility_chart_id') or config.get('boundary_chart_id') else None
            geography, levels = enrich_administrative(client, config, members, rows, geography)
            cache.set(key, (rows, geography, levels), 60)
            geometry_version = hashlib.sha256(json.dumps(geography, sort_keys=True).encode()).hexdigest()
            cache.set(key + ':geometry', geometry_version, 60)
        else:
            rows, geography, levels = snapshot
            geometry_version = cache.get(key + ':geometry')
        def matches(record, field, chosen):
            actual = values(record.get(field))
            if field in administrative_fields:
                return bool({name_key(v) for v in actual if v} & {name_key(v) for v in chosen})
            return bool(set(actual) & set(chosen))
        selected = [r for r in rows if all(matches(r, f, v) for f,v in criteria.items() if v)]
        options = {f: sorted({v for r in rows for v in values(r.get(f)) if v is not None}) for f in fields}
        # Descendants list only values in the selected parent areas; ordinary
        # selectors retain their original full-cohort option lists.
        parents = []
        for level in levels:
            field = level['field']
            parent_rows = [r for r in rows if all(not criteria.get(p) or matches(r, p, criteria[p]) for p in parents)]
            options[field] = sorted({v for r in parent_rows for v in values(r.get(field)) if v is not None})
            parents.append(field)
        breakdowns = {}
        for field in fields:
            counts = Counter(v for r in selected for v in values(r.get(field)))
            breakdowns[field] = [{field: value, 'people': count} for value,count in sorted(counts.items(),key=lambda item:str(item[0] or ''))]
        series = {}
        for panel in settings['panels']:
            for tab in panel['tabs']:
                if not tab.get('measure'):
                    continue
                grouped = {}
                for row in selected:
                    value = row.get(tab['measure'])
                    try:
                        value = float(value)
                    except (TypeError, ValueError):
                        continue
                    import math
                    if not math.isfinite(value):
                        continue
                    grouped.setdefault(str(row.get(tab['field']) or 'Not recorded'), []).append(value)
                series[tab['field'] + ':' + tab['measure'] + ':' + tab.get('aggregation', 'sum')] = [
                    {'label': key, 'value': sum(items) / len(items) if tab.get('aggregation') == 'avg' else sum(items)}
                    for key, items in sorted(grouped.items())]
        stages = [{'label': 'Current ' + settings.get('record_label', 'assessments').lower(), 'count': len(selected)}]
        cohort = selected
        for step in settings['workflow']:
            cohort = [r for r in cohort if step['value'] in values(r.get(step['field']))]
            stages.append({'label': step['label'], 'count': len(cohort)})
        geography = filter_geography(geography, settings, selected, criteria, levels)
        # The browser already has the polygons for this exact source snapshot.
        # Keep filtered area metadata/counts, and reuse its coordinates rather
        # than serialising megabytes of unchanged GeoJSON on every selection.
        if geography and geometry_version and body.get('geometry_version') == geometry_version:
            geography = {**geography, 'boundaries': [
                {k: v for k, v in boundary.items() if k != 'geometry_geojson'}
                for boundary in geography['boundaries']]}
        return JsonResponse({'geometry_version': geometry_version, 'admin_hierarchy': levels, 'geography': geography, 'rows': selected, 'total': len(rows), 'matches': len(selected),
            'sites': len({r.get(settings['site_field']) for r in selected if r.get(settings['site_field'])}),
            'countries': len({r.get(settings['country_field']) for r in selected if r.get(settings['country_field'])}),
            'options': options, 'breakdowns': breakdowns, 'series': series, 'stages': stages,
            'truncated': len(rows) >= query['row_limit']})
    except (SupersetError, ToolError) as exc:
        return _error(exc)
    except (ValueError, TypeError, KeyError):
        return JsonResponse({'error': 'Invalid workspace data or configuration.'}, status=400)
