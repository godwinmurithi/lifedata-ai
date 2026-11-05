"""Authenticated custom dashboard library, publication settings and live data."""
from __future__ import annotations

import json
from typing import Any

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt

from lifedata_ai.models import DashboardPublication
from lifedata_ai.publication_sources import is_external_app, publication_charts, validate_external_url
from lifedata_ai.superset_client import SupersetClient, SupersetError
from lifedata_ai.tools.base import ToolContext, ToolError
from lifedata_ai.tools.publications import PublishDashboardTool, validate_layout
from lifedata_ai.tools.superset_read import _parse_json_field, _prepare_query_context


def _client(request: HttpRequest) -> SupersetClient:
    return SupersetClient(session_cookie=request.superset_session_cookie)


def _error(exc: Exception) -> JsonResponse:
    if isinstance(exc, SupersetError):
        status = exc.status_code if exc.status_code in (401, 403, 404) else 502
    else:
        status = 400
    return JsonResponse({'error': str(exc)}, status=status)


def _chart_metadata(chart: dict[str, Any]) -> dict[str, Any]:
    # dashboard/<id>/charts nests viz_type in form_data on Superset 6.
    form, _ = _parse_json_field(chart.get('form_data'), 'form_data')
    form = form if isinstance(form, dict) else {}
    return {'id': chart['id'], 'title': chart.get('slice_name') or f"Chart {chart['id']}",
            'viz_type': chart.get('viz_type') or form.get('viz_type') or 'table'}


def _payload(publication: DashboardPublication, request: HttpRequest) -> dict[str, Any]:
    from lifedata_ai.quality import quality_summary
    return {
        'quality': quality_summary(publication),
        'id': str(publication.id), 'title': publication.title, 'theme': publication.theme,
        'layout': publication.layout, 'configuration': publication.configuration,
        'dashboard_id': publication.superset_dashboard_id,
        'can_edit': publication.owner_username == request.superset_identity.username,
        'url': f'/lifedata-ai/dashboards/{publication.id}/',
        'updated_at': publication.updated_at.isoformat(),
        'created_at': publication.created_at.isoformat(),
        'owner': publication.owner_username,
    }


def page(request: HttpRequest, pk=None) -> HttpResponse:
    """Serve the custom UI; APIs authenticate and authorise all data requests."""
    if request.method != 'GET':
        return JsonResponse({'error': 'method_not_allowed'}, status=405)
    response = render(request, 'lifedata_ai/dashboards.html', {'publication_id': str(pk) if pk else ''})
    # OSM requires a browser Referer. Cross-origin requests receive only our
    # origin, never publication paths, query parameters or facility details.
    response['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    return response


def library(request: HttpRequest) -> JsonResponse:
    """List only publications whose saved data sources the caller can read."""
    if request.method != 'GET':
        return JsonResponse({'error': 'method_not_allowed'}, status=405)
    client = _client(request)
    items = []
    for publication in DashboardPublication.objects.all()[:100]:
        try:
            accessible_charts = publication_charts(client, publication)
        except SupersetError as exc:
            if exc.status_code not in (401, 403, 404):
                return _error(exc)
            continue
        payload = _payload(publication, request)
        payload['chart_count'] = len(accessible_charts)
        items.append(payload)
    return JsonResponse({'items': items})


@csrf_exempt
def detail(request: HttpRequest, pk) -> JsonResponse:
    """Read a publication or persist its publisher's reordered/resized layout."""
    if request.method not in ('GET', 'PATCH'):
        return JsonResponse({'error': 'method_not_allowed'}, status=405)
    publication = DashboardPublication.objects.filter(pk=pk).first()
    if not publication:
        return JsonResponse({'error': 'not_found'}, status=404)
    try:
        client = _client(request)
        charts = publication_charts(client, publication)
    except SupersetError as exc:
        return _error(exc)
    if request.method == 'PATCH':
        if publication.owner_username != request.superset_identity.username:
            return JsonResponse({'error': 'forbidden'}, status=403)
        try:
            body = json.loads(request.body)
            layout = body['layout']
            allowed = {int(chart['id']) for chart in charts}
            layout = validate_layout(layout, allowed)
        except (ValueError, KeyError, TypeError) as exc:
            return JsonResponse({'error': str(exc)}, status=400)
        previous_snapshot = {'title': publication.title, 'theme': publication.theme,
                             'layout': publication.layout, 'configuration': publication.configuration}
        publication.layout = layout
        publication.save(update_fields=['layout', 'updated_at'])
        from lifedata_ai.quality import start_build
        start_build(ToolContext(superset_session_cookie=request.superset_session_cookie,
                               identity=request.superset_identity), publication, previous_snapshot=previous_snapshot)
    payload = _payload(publication, request)
    payload['charts'] = [_chart_metadata(chart) for chart in charts]
    return JsonResponse(payload)


def chart_data(request: HttpRequest, pk, chart_id: int) -> JsonResponse:
    """Read saved chart data through Superset using the viewer's session."""
    if request.method != 'GET':
        return JsonResponse({'error': 'method_not_allowed'}, status=405)
    publication = DashboardPublication.objects.filter(pk=pk).first()
    if not publication:
        return JsonResponse({'error': 'not_found'}, status=404)
    try:
        client = _client(request)
        charts = publication_charts(client, publication)
        if chart_id not in {int(chart['id']) for chart in charts}:
            return JsonResponse({'error': 'not_found'}, status=404)
        chart = client.get_chart(chart_id)
        context, note = _parse_json_field(chart.get('query_context'), 'query_context')
        # Both stored queries and reconstructed definitions use the same client
        # identity, membership checks, and saved limit, with a viewer ceiling.
        prepared = _prepare_query_context(context, chart, note, row_limit_cap=10000)
        from lifedata_ai.composition import apply_criteria
        apply_criteria(prepared, publication.configuration, chart_id, json.loads(request.GET.get('criteria','{}')))
        payload = client.chart_data(prepared)
    except (ValueError,TypeError):
        return JsonResponse({'error':'Invalid dashboard criteria.'},status=400)
    except (SupersetError, ToolError) as exc:
        return _error(exc)
    results = payload.get('result') or []
    result = results[0] if results else {}
    if result.get('error'):
        return JsonResponse({'error': str(result['error'])}, status=422)
    params, _ = _parse_json_field(chart.get('params'), 'params')
    return JsonResponse({
        'rows': result.get('data') or [], 'columns': result.get('colnames') or [],
        'row_limit': prepared['queries'][0]['row_limit'], 'form_data': params or {},
    })


def creatable(request: HttpRequest) -> JsonResponse:
    """List the caller's own Superset dashboards, for the homepage's dashboard picker."""
    if request.method != 'GET':
        return JsonResponse({'error': 'method_not_allowed'}, status=405)
    try:
        dashboards_list = _client(request).list_dashboards(request.GET.get('q') or None)
    except SupersetError as exc:
        return _error(exc)
    published = set(
        DashboardPublication.objects.filter(owner_username=request.superset_identity.username)
        .values_list('superset_dashboard_id', flat=True)
    )
    items = [
        {'id': item['id'], 'title': item.get('dashboard_title') or f"Dashboard {item['id']}",
         'already_published': item['id'] in published}
        for item in dashboards_list
    ]
    return JsonResponse({'items': items})


@csrf_exempt
def create(request: HttpRequest) -> JsonResponse:
    """Publish a Superset dashboard the caller already owns or can see, from the homepage."""
    if request.method != 'POST':
        return JsonResponse({'error': 'method_not_allowed'}, status=405)
    try:
        body = json.loads(request.body)
        dashboard_id = int(body['dashboard_id'])
        title = str(body['title']).strip()
        theme = body.get('theme') or 'light'
        if not title or theme not in ('light', 'dark'):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        return JsonResponse({'error': 'invalid_request'}, status=400)
    context = ToolContext(
        superset_session_cookie=request.superset_session_cookie,
        identity=request.superset_identity,
    )
    try:
        result = PublishDashboardTool().run(context, dashboard_id=dashboard_id, title=title, theme=theme)
    except (SupersetError, ToolError) as exc:
        return _error(exc)
    publication = DashboardPublication.objects.get(id=result['publication_id'])
    return JsonResponse(_payload(publication, request), status=201)


@csrf_exempt
def external(request: HttpRequest) -> JsonResponse:
    """Create or update an external-app publication (owner-scoped, no Superset sources)."""
    if request.method != 'POST':
        return JsonResponse({'error': 'method_not_allowed'}, status=405)
    from django.core.exceptions import ValidationError
    try:
        body = json.loads(request.body)
        publication_id = body.get('publication_id')
        title = str(body['title']).strip()
        url = str(body['url']).strip()
        theme = body.get('theme') or 'light'
        if not title or theme not in ('light', 'dark'):
            raise ValueError
        url = validate_external_url(url)
    except (ValueError, KeyError, TypeError):
        return JsonResponse({'error': 'invalid_request'}, status=400)
    except ToolError as exc:
        return JsonResponse({'error': str(exc)}, status=400)
    username = request.superset_identity.username
    if publication_id:
        try:
            publication = DashboardPublication.objects.filter(
                pk=publication_id, owner_username=username).first()
        except (ValueError, ValidationError):
            return JsonResponse({'error': 'invalid_request'}, status=400)
        if not publication or not is_external_app(publication):
            return JsonResponse({'error': 'not_found'}, status=404)
    else:
        publication = DashboardPublication(owner_username=username, superset_dashboard_id=None, layout=[])
    publication.title = title
    publication.theme = theme
    publication.configuration = {'preset': 'external-app', 'url': url}
    publication.save()
    return JsonResponse(_payload(publication, request), status=200 if publication_id else 201)


@csrf_exempt
def delete(request: HttpRequest, pk) -> JsonResponse:
    """Delete an external-app publication (owner only)."""
    if request.method != 'DELETE':
        return JsonResponse({'error': 'method_not_allowed'}, status=405)
    publication = DashboardPublication.objects.filter(pk=pk).first()
    if not publication or not is_external_app(publication):
        return JsonResponse({'error': 'not_found'}, status=404)
    if publication.owner_username != request.superset_identity.username:
        return JsonResponse({'error': 'forbidden'}, status=403)
    publication.delete()
    return JsonResponse({'deleted': str(pk)})
