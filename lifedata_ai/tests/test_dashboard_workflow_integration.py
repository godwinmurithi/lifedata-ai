"""The publisher, exact reference and quality lifecycle share real contracts."""
import copy
import json
from unittest.mock import Mock

import pytest
from django.test import Client, RequestFactory

from lifedata_ai.middleware import SupersetIdentity
from lifedata_ai.models import DashboardBuild, DashboardPublication
from lifedata_ai.publication_sources import configuration_chart_ids
from lifedata_ai.superset_client import SupersetError
from lifedata_ai.tools.base import ToolContext, ToolError
from lifedata_ai.tools.publications import PublishDashboardTool, EditDashboardChartStyleTool
from lifedata_ai.views import dashboards, templates, composition

pytestmark = pytest.mark.django_db


def fixture():
    client = Mock()
    client.get_chart.side_effect = lambda identifier: {
        'id': identifier, 'viz_type': 'big_number_total', 'slice_name': 'Distinct people',
        'params': {'viz_type': 'big_number_total', 'datasource': '42__table'},
        'query_context': {'datasource': {'id': 42, 'type': 'table'}, 'queries': [{
            'columns': [], 'metrics': ['count'], 'row_limit': 100,
            'filters': [{'col': 'tenant', 'op': '==', 'val': 'current'}]}]},
    }
    client.get_dataset.return_value = {'columns': [{'column_name': 'district'}, {'column_name': 'sex'}]}
    context = ToolContext(superset_session_cookie='test-session', identity=SupersetIdentity(username='ana'))
    context._client = client
    config = {'preset': 'reference-template', 'source_chart_id': 12, 'chart_ids': [12],
              'template': 'sincep-hdss', 'bindings': {'residents': {'chart_ids': [12]}},
              'composition': {'purpose': 'overview'}}
    return context, client, config


def request(path='/', query=None):
    result = RequestFactory().get(path, query or {})
    result.superset_identity = SupersetIdentity(username='ana')
    result.superset_session_cookie = 'test-session'
    return result


def test_exact_template_publishes_draft_with_persistent_brief_and_restorable_revision(monkeypatch):
    context, client, config = fixture()
    brief = {'audience': 'Demo users', 'questions': ['How many distinct people?'],
             'template': 'sincep-hdss', 'viewports': [{'width': 1366, 'height': 768}]}
    result = PublishDashboardTool().run(context, title='Current records', configuration=config, specification=brief)
    publication = DashboardPublication.objects.get(pk=result['publication_id'])
    build = DashboardBuild.objects.get(pk=result['build_id'])
    assert result['quality_status'] == build.status == 'draft'
    assert build.specification == brief
    assert publication.configuration['bindings'] == config['bindings']
    previous = copy.deepcopy(publication.configuration)
    result = EditDashboardChartStyleTool().run(context, publication_id=str(publication.pk),
        target='chart:12', style={'color': '#123456'})
    build.refresh_from_db()
    assert result['build_id'] == str(build.pk)
    assert build.previous_revision['configuration'] == previous
    monkeypatch.setattr(dashboards, '_client', lambda _: client)
    payload = json.loads(dashboards.detail(request(), publication.pk).content)
    assert payload['quality']['status'] == 'draft'


def test_reference_document_reauthorizes_sources_and_does_not_inject_titles(monkeypatch):
    context, client, config = fixture()
    result = PublishDashboardTool().run(context, title='</title><script>alert(1)</script>', configuration=config)
    monkeypatch.setattr(templates, '_client', lambda _: client)
    response = templates.document(request(), result['publication_id'])
    assert response.status_code == 200
    assert response['Cache-Control'] == 'private, no-store'
    assert '<title></title><script>alert(1)</script>' not in response.content.decode()
    assert b'vima-reference-data' in response.content
    client.get_chart.side_effect = SupersetError('Forbidden', status_code=403)
    assert templates.document(request(), result['publication_id']).status_code == 403


def test_reference_endpoints_require_login_and_only_registered_template_names():
    assert Client().get('/lifedata-ai/dashboard-template-preview/sincep-hdss/').status_code == 302
    assert Client().get('/lifedata-ai/api/dashboard-templates/sincep-hdss/source/').status_code == 401
    assert templates.preview(request(), 'unknown').status_code == 404
    response = templates.source(request(), 'sincep-hdss')
    assert response['ETag'] == '"37f8fb2337afbaae241b3e7e3ca7e030606f3da48f6bbb709b9abbdb16cc0863"'


def test_geographic_helper_sources_are_in_saved_membership():
    config = {'source_chart_id': 12, 'chart_ids': [12], 'bindings': {'event_map': {
        'chart_ids': [12], 'geography': {'source_chart_id': 13,
            'administrative_boundaries': [{'chart_id': 14}]}}}}
    assert configuration_chart_ids(config) == [12, 13, 14]


def test_reference_map_joins_real_coordinates_and_returns_area_aggregates(monkeypatch):
    from lifedata_ai.tests.test_administrative import setup_sources
    client, workspace_config, _ = setup_sources()
    context = ToolContext(superset_session_cookie='test-session', identity=SupersetIdentity(username='ana'))
    context._client = client
    config = {'preset': 'reference-template', 'source_chart_id': 12, 'chart_ids': [12, 13],
              'template': 'sincep-hdss', 'composition': {'purpose': 'geographic'},
              'bindings': {'event_map': {'chart_ids': [12], 'geography': {
                  'source_chart_id': 12, 'site_field': 'facility_name',
                  'administrative_boundaries': workspace_config['administrative_boundaries']}}}}
    result = PublishDashboardTool().run(context, title='Joined map', configuration=config)
    monkeypatch.setattr(templates, '_client', lambda _: client)
    response = templates.map_data(request(), result['publication_id'], 'event_map')
    assert response.status_code == 200
    payload = json.loads(response.content)
    assert payload['location_basis'] == 'matched_administrative_polygons'
    assert len(payload['rows']) == 6
    assert all(area['record_count'] == 1 for area in payload['rows'])
    assert payload['join_diagnostics']['matched_records'] == 2
    assert all('facility_name' not in area for area in payload['rows'])
    assert templates.map_data(request(), result['publication_id'], 'unbound').status_code == 404


def test_unsupported_visual_is_rejected_before_saving_a_false_map():
    context, client, config = fixture()
    client.get_chart.side_effect = lambda identifier: {'id': identifier, 'viz_type': 'deck_multi'}
    config = {'preset': 'composed-dashboard', 'source_chart_id': 12, 'chart_ids': [12],
              'composition': {'purpose': 'geographic'}}
    with pytest.raises(ToolError) as exc:
        PublishDashboardTool().run(context, title='Map', configuration=config)
    assert exc.value.code == 'unsupported_renderer'
    assert not DashboardPublication.objects.exists()


def test_cascading_filter_options_are_aggregate_and_keep_saved_scope(monkeypatch):
    context, client, config = fixture()
    config['composition']['filters'] = [
        {'field': 'district', 'label': 'District', 'chart_ids': [12]},
        {'field': 'sex', 'label': 'Sex', 'chart_ids': [12]},
    ]
    result = PublishDashboardTool().run(context, title='Current records', configuration=config)
    client.chart_data.return_value = {'result': [{'data': [{'district': 'A', 'sex': 'Female'}]}]}
    monkeypatch.setattr(composition, '_client', lambda _: client)
    response = composition.filter_options(request(query={'criteria': json.dumps({'district': ['A']})}), result['publication_id'])
    assert response.status_code == 200
    contexts = [call.args[0] for call in client.chart_data.call_args_list]
    assert all(query['queries'][0]['metrics'][0]['sqlExpression'] == 'COUNT(*)' for query in contexts)
    assert contexts[0]['queries'][0]['filters'] == [{'col': 'tenant', 'op': '==', 'val': 'current'}]
    assert contexts[1]['queries'][0]['filters'][-1] == {'col': 'district', 'op': 'IN', 'val': ['A']}
