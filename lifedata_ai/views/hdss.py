"""HTTP endpoints of the hdss-site template.

POST /lifedata-ai/dashboards/<uuid>/query      filter state -> per-card aggregates (never rows)
GET  /lifedata-ai/api/hdss/templates/           the template and the registered datasets you can read
POST /lifedata-ai/api/hdss/dashboards/          create a DRAFT from a registered dataset
POST /lifedata-ai/api/hdss/dashboards/<uuid>/status/   owner publishes or returns to draft

Every request runs as the viewer: Superset checks the dataset permission on each aggregate.
"""
from __future__ import annotations

import json

from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from lifedata_ai.hdss import engine
from lifedata_ai.hdss.registry import BindingsError, load_bindings
from lifedata_ai.hdss.service import authorise, create as create_dashboard, is_hdss, page_spec, set_status, visible_to
from lifedata_ai.hdss.spec import DEEP_DIVE_CARDS, SLOTS, FILTERS, SpecError, coverage, build_spec, validate_spec
from lifedata_ai.models import DashboardPublication
from lifedata_ai.superset_client import SupersetError
from lifedata_ai.views.dashboards import _client, _error

CACHE_SECONDS = 300


def _publication(request, pk):
    publication = DashboardPublication.objects.filter(pk=pk).first()
    if not publication or not is_hdss(publication) or not visible_to(publication, request.superset_identity):
        return None
    return publication


def _body(request):
    try:
        return json.loads(request.body or b'{}')
    except ValueError:
        return None


@csrf_exempt
def query(request, pk):
    if request.method != 'POST':
        return JsonResponse({'error': 'method_not_allowed'}, status=405)
    publication = _publication(request, pk)
    if publication is None:
        return JsonResponse({'error': 'not_found'}, status=404)
    body = _body(request)
    if not isinstance(body, dict):
        return JsonResponse({'error': 'The request body must be a JSON object.'}, status=400)
    client = _client(request)
    try:
        authorise(client, publication)  # every request, before any cached answer is served
        spec = page_spec(publication)
        binding, _ = validate_spec(publication.configuration['spec'])
    except SupersetError as exc:
        return _error(exc)
    except (SpecError, BindingsError) as exc:
        return JsonResponse({'error': str(exc)}, status=409)
    execute = engine.superset_executor(client)
    stamp = f'{publication.pk}:{publication.updated_at.isoformat()}'
    try:
        prof = cache.get(f'hdss:profile:{stamp}')
        if prof is None:
            prof = engine.profile(binding, execute)
            cache.set(f'hdss:profile:{stamp}', prof, CACHE_SECONDS)
        state = engine.parse_state(body.get('state'), binding, prof['paths'], spec['filters'])
        key = f'hdss:q:{stamp}:{state.digest()}'
        data = cache.get(key)
        if data is None:
            # Small datasets: one grouped "cube" per dashboard, then every click is computed in memory.
            cube = cache.get(f'hdss:cube:{stamp}')
            if cube is None:
                cube = engine.load_cube(binding, execute) or False  # False = too large, use Superset per state
                cache.set(f'hdss:cube:{stamp}', cube, CACHE_SECONDS)
            run_with = engine.cube_executor(cube, binding) if cube else execute
            data = engine.run(binding, state, spec['slots'], run_with, prof)
            cache.set(key, data, CACHE_SECONDS)
    except engine.StateError as exc:
        return JsonResponse({'error': str(exc)}, status=400)
    except SupersetError as exc:
        return _error(exc)
    except RuntimeError as exc:
        return JsonResponse({'error': str(exc)}, status=502)
    return JsonResponse({'state': state.canonical(), 'data': data, 'illustrative': binding.synthetic})


def spec(request, pk):
    """The effective spec for the page (unbound slots dropped, with reasons)."""
    if request.method != 'GET':
        return JsonResponse({'error': 'method_not_allowed'}, status=405)
    publication = _publication(request, pk)
    if publication is None:
        return JsonResponse({'error': 'not_found'}, status=404)
    try:
        authorise(_client(request), publication)
        out = page_spec(publication)
    except SupersetError as exc:
        return _error(exc)
    except SpecError as exc:
        return JsonResponse({'error': str(exc)}, status=409)
    out['can_edit'] = publication.owner_username == request.superset_identity.username
    return JsonResponse(out)


def templates(request):
    if request.method != 'GET':
        return JsonResponse({'error': 'method_not_allowed'}, status=405)
    client = _client(request)
    datasets = []
    for binding in load_bindings().values():
        try:
            if not client.get_dataset(binding.superset_dataset_id):
                continue
        except SupersetError as exc:
            if exc.status_code in (401, 403, 404):
                continue
            return _error(exc)
        report = coverage(binding, build_spec(binding, binding.label))
        datasets.append({'key': binding.key, 'label': binding.label, 'superset_dataset_id': binding.superset_dataset_id,
                         'site': binding.site_value, 'synthetic': binding.synthetic,
                         'bound_slots': sum(1 for v in report['slots'].values() if v == 'bound'),
                         'slots': len(SLOTS)})
    return JsonResponse({'templates': [{
        'id': 'hdss-site', 'title': 'HDSS dashboard',
        'description': 'The approved HDSS site page: KPI cards with deep dives, cascading geography, and every card a filter. '
                       'Built from a registered dataset; unbound cards are hidden, never invented.',
        'slots': list(SLOTS), 'filters': list(FILTERS), 'deep_dive_cards': list(DEEP_DIVE_CARDS), 'datasets': datasets}]})


@csrf_exempt
def create(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'method_not_allowed'}, status=405)
    body = _body(request)
    if not isinstance(body, dict) or not isinstance(body.get('dataset'), str) or not isinstance(body.get('title'), str):
        return JsonResponse({'error': 'Send {dataset, title}.'}, status=400)
    try:
        result = create_dashboard(_client(request), request.superset_identity.username, body['dataset'],
                                  body['title'].strip(), body.get('deep_dive'),
                                  population_reference=body.get('population_reference'))
    except SupersetError as exc:
        return _error(exc)
    except (SpecError, BindingsError) as exc:
        return JsonResponse({'error': str(exc)}, status=400)
    return JsonResponse(result, status=201)


@csrf_exempt
def status(request, pk):
    if request.method != 'POST':
        return JsonResponse({'error': 'method_not_allowed'}, status=405)
    body = _body(request)
    if not isinstance(body, dict):
        return JsonResponse({'error': 'Send {status}.'}, status=400)
    try:
        result = set_status(request.superset_identity.username, str(pk), body.get('status'))
    except SpecError as exc:
        return JsonResponse({'error': str(exc)}, status=400)
    return JsonResponse(result)
