"""Published custom pages keep chart membership, access and saved layouts intact."""
from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from django.test import RequestFactory

from lifedata_ai.middleware import SupersetIdentity
from lifedata_ai.models import DashboardPublication
from lifedata_ai.superset_client import SupersetError
from lifedata_ai.tools.base import ToolContext
from lifedata_ai.tools.publications import PublishDashboardTool
from lifedata_ai.views import dashboards

pytestmark = pytest.mark.django_db


def setup_publication(monkeypatch):
    client = Mock()
    client.get_dashboard.return_value = {'id': 4}
    client.get_dashboard_charts.return_value = [{'id': 12, 'slice_name': 'Visits', 'viz_type': 'table'}, {'id': 13, 'slice_name': 'Results', 'viz_type': 'pie'}]
    context = ToolContext(superset_session_cookie='dummy', identity=SupersetIdentity(username='ana'))
    context._client = client
    result = PublishDashboardTool().run(context, dashboard_id=4, title='Service dashboard', theme='light', layout=[{'chart_id': 12, 'span': 4, 'height': 240}, {'chart_id': 13, 'span': 4, 'height': 240}])
    monkeypatch.setattr(dashboards, '_client', lambda request: client)
    return context, client, DashboardPublication.objects.get(id=result['publication_id'])


def make_request(method='GET', body=None, username='ana'):
    request = RequestFactory().generic(method, '/lifedata-ai/api/publications/', json.dumps(body or {}), content_type='application/json')
    request.superset_identity = SupersetIdentity(username=username)
    request.superset_session_cookie = 'dummy'
    return request


def test_facility_preset_sources_must_belong_to_dashboard(monkeypatch):
    from lifedata_ai.tools.base import ToolError
    context, _, publication = setup_publication(monkeypatch)
    configuration = {'preset': 'facility-network', 'source_chart_id': 12, 'network_chart_id': 999}
    with pytest.raises(ToolError, match='Facility sources'):
        PublishDashboardTool().run(context, 4, 'Facilities', 'light', configuration=configuration)
    configuration['network_chart_id'] = 13
    PublishDashboardTool().run(context, 4, 'Facilities', 'light', configuration=configuration)
    PublishDashboardTool().run(context, 4, 'Facilities updated', 'light')
    publication.refresh_from_db()
    assert publication.configuration == {**configuration, 'design': {'baseline': 'vima-adaptive-v1'}}
    assert json.loads(dashboards.detail(make_request(), publication.id).content)['configuration'] == publication.configuration


def test_dashboard_page_sends_only_origin_to_cross_site_basemap():
    response = dashboards.page(RequestFactory().get('/lifedata-ai/dashboards/'))
    assert response['Referrer-Policy'] == 'strict-origin-when-cross-origin'


def test_facility_preset_rejects_invalid_map_bounds(monkeypatch):
    from lifedata_ai.tools.base import ToolError
    context, _, _ = setup_publication(monkeypatch)
    configuration = {'preset': 'facility-network', 'source_chart_id': 12, 'network_chart_id': 13,
                     'map_bounds': [95, 34, 1, 35]}
    with pytest.raises(ToolError, match='Map bounds'):
        PublishDashboardTool().run(context, 4, 'Facilities', 'light', configuration=configuration)


def test_publish_republish_preserves_edited_layout(monkeypatch):
    context, _, publication = setup_publication(monkeypatch)
    publication.layout = [{'chart_id': 13, 'span': 12, 'height': 480}, {'chart_id': 12, 'span': 6, 'height': 320}]
    publication.save()
    result = PublishDashboardTool().run(context, dashboard_id=4, title='Updated', theme='dark')
    publication.refresh_from_db()
    assert result['publication_id'] == str(publication.id)
    assert publication.title == 'Updated'
    assert publication.layout[0] == {'chart_id': 13, 'span': 12, 'height': 480}
    assert DashboardPublication.objects.count() == 1


def test_layout_requires_publisher_and_exact_chart_membership(monkeypatch):
    _, _, publication = setup_publication(monkeypatch)
    assert dashboards.detail(make_request('PATCH', {'layout': publication.layout}, 'bob'), publication.id).status_code == 403
    bad = [{'chart_id': 12, 'span': 6, 'height': 320}] * 2
    assert dashboards.detail(make_request('PATCH', {'layout': bad}), publication.id).status_code == 400
    layout = list(reversed(publication.layout))
    assert dashboards.detail(make_request('PATCH', {'layout': layout}), publication.id).status_code == 200
    publication.refresh_from_db()
    assert publication.layout == layout


def test_source_permissions_still_apply_and_chart_cannot_be_swapped(monkeypatch):
    _, client, publication = setup_publication(monkeypatch)
    assert dashboards.chart_data(make_request(), publication.id, 99).status_code == 404
    client.get_dashboard.side_effect = SupersetError('Access denied', status_code=403)
    assert dashboards.detail(make_request(), publication.id).status_code == 403
    assert json.loads(dashboards.library(make_request()).content)['items'] == []


def test_viewer_reads_all_saved_dummy_rows_without_model_excerpt_limits(monkeypatch):
    _, client, publication = setup_publication(monkeypatch)
    client.get_chart.return_value = {
        'id': 12, 'params': {'viz_type': 'table', 'query_mode': 'raw'},
        'query_context': {'datasource': {'id': 42, 'type': 'table'}, 'form_data': {'query_mode': 'raw'},
                          'queries': [{'columns': ['person_id'], 'row_limit': 299}]},
    }
    rows = [{'person_id': f'dummy-{index}'} for index in range(299)]
    client.chart_data.return_value = {'result': [{'data': rows, 'colnames': ['person_id']}]}
    response = dashboards.chart_data(make_request(), publication.id, 12)
    assert response.status_code == 200
    assert len(json.loads(response.content)['rows']) == 299
    assert client.chart_data.call_args.args[0]['queries'][0]['row_limit'] == 299


def test_chatbot_can_publish_a_reference_layout(monkeypatch):
    context, _, publication = setup_publication(monkeypatch)
    layout = [{'chart_id': 13, 'span': 4, 'height': 240}, {'chart_id': 12, 'span': 12, 'height': 480}]
    PublishDashboardTool().run(context, dashboard_id=4, title='Reference layout', theme='dark', layout=layout)
    publication.refresh_from_db()
    assert publication.layout == layout


@pytest.mark.parametrize('path', ['/lifedata-ai/', '/lifedata-ai/dashboards', '/lifedata-ai/dashboards/'])
def test_dashboard_entry_points_redirect_to_login_without_a_session(path):
    from django.test import Client
    response = Client().get(path)
    assert response.status_code == 302
    assert response['Location'] == '/login/?next=%2Flifedata-ai%2Fdashboards%2F'


def test_dashboard_login_fix_keeps_api_requests_as_json_401():
    from django.test import Client
    response = Client().get('/lifedata-ai/api/publications/')
    assert response.status_code == 401
    assert response.json()['error'] == 'unauthenticated'


def test_design_defaults_and_republish_preserve_colors_and_groups(monkeypatch):
    context, _, publication = setup_publication(monkeypatch)
    assert publication.configuration['design']['baseline'] == 'vima-adaptive-v1'
    design = {'colors': {'yes': '#2563eb', 'no': '#f59e0b'},
              'groups': [{'title': 'Responses', 'chart_ids': [12, 13]}]}
    PublishDashboardTool().run(context, 4, 'Responses', 'light', design=design)
    PublishDashboardTool().run(context, 4, 'Responses updated', 'dark')
    publication.refresh_from_db()
    assert publication.configuration['design'] == {'baseline': 'vima-adaptive-v1', **design}
    assert json.loads(dashboards.detail(make_request(), publication.id).content)['configuration']['design'] == publication.configuration['design']


@pytest.mark.parametrize('design', [
    {'groups': [{'title': 'Foreign chart', 'chart_ids': [12, 999]}]},
    {'groups': [{'title': 'Duplicate', 'chart_ids': [12, 12]}]},
    {'colors': {'yes': 'url(https://example.com)'}},
    {'baseline': 'unrecognized'},
])
def test_design_rejects_foreign_duplicate_charts_and_unsafe_colors(monkeypatch, design):
    from lifedata_ai.tools.base import ToolError
    context, _, publication = setup_publication(monkeypatch)
    with pytest.raises(ToolError):
        PublishDashboardTool().run(context, 4, 'Unsafe', 'light', design=design)
    publication.refresh_from_db()
    assert publication.title == 'Service dashboard'


def test_custom_viewer_runs_chart_params_without_query_context(monkeypatch):
    _, client, publication = setup_publication(monkeypatch)
    client.get_chart.return_value = {'id': 13, 'datasource_id': 210, 'viz_type': 'pie',
        'query_context': None, 'params': json.dumps({'viz_type': 'pie', 'groupby': ['confirm_safe'],
                                                    'metrics': ['count'], 'row_limit': 700})}
    client.chart_data.return_value = {'result': [{'data': [{'confirm_safe': 'Yes', 'count': 8}],
                                                 'colnames': ['confirm_safe', 'count']}]}
    response = dashboards.chart_data(make_request(), publication.id, 13)
    assert response.status_code == 200
    body = json.loads(response.content)
    assert body['rows'] == [{'confirm_safe': 'Yes', 'count': 8}]
    query = client.chart_data.call_args.args[0]
    assert query['datasource']['id'] == 210
    assert query['queries'][0]['columns'] == ['confirm_safe']
    assert query['queries'][0]['row_limit'] == 700


def test_custom_viewer_does_not_run_foreign_chart_even_with_valid_params(monkeypatch):
    _, client, publication = setup_publication(monkeypatch)
    assert dashboards.chart_data(make_request(), publication.id, 999).status_code == 404
    client.chart_data.assert_not_called()


def test_chart_type_is_read_from_nested_superset_form_data(monkeypatch):
    _, client, publication = setup_publication(monkeypatch)
    client.get_dashboard_charts.return_value = [{'id': 12, 'slice_name': 'Current client responses',
        'form_data': {'viz_type': 'echarts_timeseries_bar'}}]
    response = dashboards.detail(make_request(), publication.id)
    assert json.loads(response.content)['charts'][0]['viz_type'] == 'echarts_timeseries_bar'


def test_publication_refuses_to_silently_omit_source_layout_charts(monkeypatch):
    from lifedata_ai.tools.base import ToolError
    context, client, publication = setup_publication(monkeypatch)
    client.get_dashboard.return_value = {'id': 4, 'position_json': {
        'ROOT_ID': {'children': ['CHART-99']},
        'CHART-99': {'type': 'CHART', 'meta': {'chartId': 99}, 'children': []}}}
    with pytest.raises(ToolError) as exc:
        PublishDashboardTool().run(context, 4, 'Incomplete', 'light')
    assert exc.value.code == 'incomplete_dashboard_chart_membership'
    assert exc.value.details['missing_chart_ids'] == [99]
    publication.refresh_from_db()
    assert publication.title == 'Service dashboard'


def test_republishing_adds_all_new_source_charts_preserving_existing_cards(monkeypatch):
    context, client, publication = setup_publication(monkeypatch)
    edited = [{'chart_id': 13, 'span': 12, 'height': 480}, {'chart_id': 12, 'span': 4, 'height': 240}]
    publication.layout = edited
    publication.save()
    client.get_dashboard_charts.return_value += [{'id': 99, 'slice_name': 'Additional question'}]
    result = PublishDashboardTool().run(context, 4, 'Complete', 'light')
    publication.refresh_from_db()
    assert result['chart_count'] == 3
    assert publication.layout[:2] == edited
    assert publication.layout[2]['chart_id'] == 99


def test_generic_publication_can_reuse_its_saved_configuration(monkeypatch):
    context, _, publication = setup_publication(monkeypatch)
    PublishDashboardTool().run(context, 4, 'Reused configuration', 'light', configuration=publication.configuration)
    publication.refresh_from_db()
    assert publication.configuration == {'design': {'baseline': 'vima-adaptive-v1'}}


def test_new_publication_requires_default_workspace_configuration():
    from lifedata_ai.tools.base import ToolError
    client = Mock()
    client.get_dashboard.return_value = {'id': 77}
    client.get_dashboard_charts.return_value = [{'id': 12}]
    context = ToolContext(superset_session_cookie='dummy', identity=SupersetIdentity(username='new-owner'))
    context._client = client
    with pytest.raises(ToolError) as error:
        PublishDashboardTool().run(context, 77, 'New dashboard', 'light')
    assert error.value.code == 'workspace_configuration_required'
    assert not DashboardPublication.objects.filter(superset_dashboard_id=77).exists()


def test_workspace_accepts_owned_geographic_sources_and_rejects_foreign_ones(monkeypatch):
    from lifedata_ai.tools.base import ToolError
    context,_,publication=setup_publication(monkeypatch)
    config={'preset':'buikwe-workspace','source_chart_id':12,'facility_chart_id':12,'boundary_chart_id':13,'region_label':'Client region',
        'workspace':{'selectors':['county'],'site_field':'site','country_field':'','county_field':'county','subcounty_field':'subcounty',
            'record_columns':['site'],'panels':[{'slot':'blood','title':'Service','tabs':[{'field':'service','label':'Service'}]}],'workflow':[]}}
    PublishDashboardTool().run(context,4,'Client workspace','light',configuration=config)
    publication.refresh_from_db()
    assert publication.configuration['facility_chart_id']==12
    assert publication.configuration['boundary_chart_id']==13
    config['boundary_chart_id']=999
    with pytest.raises(ToolError,match='Geographic sources'):
        PublishDashboardTool().run(context,4,'Invalid workspace','light',configuration=config)


def test_numeric_workspace_tabs_have_independent_style_targets(monkeypatch):
    from lifedata_ai.tools.publications import EditDashboardChartStyleTool
    context, _, publication = setup_publication(monkeypatch)
    publication.configuration = {'preset': 'buikwe-workspace', 'workspace': {'panels': [
        {'title': 'Staffing', 'tabs': [
            {'field': 'level', 'measure': 'clinicians', 'label': 'Clinicians'},
            {'field': 'level', 'measure': 'lab_staff', 'label': 'Lab staff'}]}]}}
    publication.save()
    tool = EditDashboardChartStyleTool()
    tool.run(context, 4, target='field:level:clinicians:sum', style={'color': '#16a34a'})
    publication.refresh_from_db()
    assert publication.configuration['design']['chart_styles'] == {
        'field:level:clinicians:sum': {'color': '#16a34a'}}
    tool.run(context, 4, target='field:level:lab_staff:sum', style={'color': '#2563eb'})
    publication.refresh_from_db()
    assert publication.configuration['design']['chart_styles']['field:level:clinicians:sum'] == {'color': '#16a34a'}
    assert publication.configuration['design']['chart_styles']['field:level:lab_staff:sum'] == {'color': '#2563eb'}


def standalone_context():
    client = Mock()
    client.get_chart.side_effect = lambda identifier: {'id': identifier, 'slice_name': f'Source {identifier}', 'viz_type': 'table',
        'params': {'query_mode': 'raw'}, 'query_context': {'datasource': {'id': 210, 'type': 'table'},
        'queries': [{'columns': ['site', 'country', 'ready'], 'metrics': [], 'row_limit': 10000}]}}
    client.get_dataset.return_value = {'columns': [{'column_name': c} for c in ['site', 'country', 'ready']]}
    client.chart_data.return_value = {'result': [{'data': [{'site': 'A', 'country': 'Uganda', 'ready': 'yes'}]}]}
    context = ToolContext(superset_session_cookie='dummy', identity=SupersetIdentity(username='ana'))
    context._client = client
    config = {'preset': 'buikwe-workspace', 'source_chart_id': 12, 'workspace': {
        'selectors': ['country'], 'site_field': 'site', 'country_field': 'country',
        'record_columns': ['site', 'country'], 'panels': [{'slot': 'blood', 'title': 'Readiness',
        'tabs': [{'field': 'ready', 'label': 'Ready'}]}], 'workflow': []}}
    return context, client, config


def test_custom_only_publisher_never_requires_or_creates_a_native_dashboard(monkeypatch):
    from lifedata_ai.tools.publications import EditDashboardChartStyleTool
    from lifedata_ai.views import workspace
    context, client, config = standalone_context()
    result = PublishDashboardTool().run(context, title='Custom only', theme='light', configuration=config)
    publication = DashboardPublication.objects.get(pk=result['publication_id'])
    assert publication.superset_dashboard_id is None
    assert publication.layout == [{'chart_id': 12, 'span': 6, 'height': 320}]
    monkeypatch.setattr(dashboards, '_client', lambda request: client)
    monkeypatch.setattr(workspace, '_client', lambda request: client)
    assert dashboards.detail(make_request(), publication.id).status_code == 200
    assert json.loads(dashboards.library(make_request()).content)['items'][0]['dashboard_id'] is None
    response = workspace.data(make_request(), publication.id)
    assert json.loads(response.content)['matches'] == 1
    assert dashboards.chart_data(make_request(), publication.id, 12).status_code == 200
    assert dashboards.chart_data(make_request(), publication.id, 999).status_code == 404
    EditDashboardChartStyleTool().run(context, publication_id=result['publication_id'], target='gauge', style={'color': '#16a34a'})
    publication.refresh_from_db()
    assert publication.configuration['design']['chart_styles']['gauge']['color'] == '#16a34a'
    updated = PublishDashboardTool().run(context, publication_id=result['publication_id'], title='Renamed', theme='dark')
    assert updated['publication_id'] == result['publication_id']
    assert DashboardPublication.objects.count() == 1
    publication.refresh_from_db()
    assert publication.configuration['design']['chart_styles']['gauge']['color'] == '#16a34a'
    client.get_dashboard.assert_not_called()
    client.get_dashboard_charts.assert_not_called()
    client._request.assert_not_called()


def test_custom_only_publications_are_distinct_and_preserve_viewer_permissions(monkeypatch):
    context, client, config = standalone_context()
    first = PublishDashboardTool().run(context, title='First', theme='light', configuration=config)
    second = PublishDashboardTool().run(context, title='Second', theme='light', configuration=config)
    assert first['publication_id'] != second['publication_id']
    assert DashboardPublication.objects.count() == 2
    other = ToolContext(superset_session_cookie='dummy', identity=SupersetIdentity(username='someone_else'))
    other._client = client
    from lifedata_ai.tools.base import ToolError
    with pytest.raises(ToolError):
        PublishDashboardTool().run(other, publication_id=first['publication_id'], title='Hijack', theme='light')
    client.get_chart.side_effect = SupersetError('Access denied', status_code=403)
    monkeypatch.setattr(dashboards, '_client', lambda request: client)
    assert dashboards.detail(make_request(), first['publication_id']).status_code == 403
    assert dashboards.chart_data(make_request(), first['publication_id'], 12).status_code == 403
    assert json.loads(dashboards.library(make_request()).content)['items'] == []
    with pytest.raises(SupersetError):
        PublishDashboardTool().run(context, title='Forbidden', theme='light', configuration=config)
    assert DashboardPublication.objects.count() == 2


def test_facility_preset_allows_source_without_network(monkeypatch):
    context, _, publication = setup_publication(monkeypatch)
    configuration = {'preset': 'facility-network', 'source_chart_id': 12}
    PublishDashboardTool().run(context, 4, 'Facilities without PCN', 'light', configuration=configuration)
    publication.refresh_from_db()
    assert publication.configuration['source_chart_id'] == 12
    assert 'network_chart_id' not in publication.configuration


def test_external_app_publish_and_url_validation(monkeypatch):
    from lifedata_ai.tools.base import ToolError
    context, client, _ = setup_publication(monkeypatch)
    before_dash, before_chart = client.get_dashboard.call_count, client.get_chart.call_count
    result = PublishDashboardTool().run(context, title='CCBU EHS', theme='light',
                                        configuration={'preset': 'external-app', 'url': 'https://dashboards.lifehealth.app/ccbu-ehs'})
    publication = DashboardPublication.objects.get(id=result['publication_id'])
    assert publication.superset_dashboard_id is None
    assert publication.configuration == {'preset': 'external-app', 'url': 'https://dashboards.lifehealth.app/ccbu-ehs'}
    assert publication.layout == []
    # No Superset calls for an external application.
    assert client.get_dashboard.call_count == before_dash
    assert client.get_chart.call_count == before_chart
    # Rejects non-https, credentials, and unapproved hosts.
    for url in ('http://dashboards.lifehealth.app/x', 'javascript:alert(1)',
                'https://user:pass@dashboards.lifehealth.app/x', 'https://evil.example.com/x'):
        with pytest.raises(ToolError, match='External app'):
            PublishDashboardTool().run(context, title='Bad', theme='light',
                                       configuration={'preset': 'external-app', 'url': url})
    # An imported (chart-backed) publication cannot become an external application.
    _, _, imported = setup_publication(monkeypatch)
    with pytest.raises(ToolError, match='publish without a Superset dashboard'):
        PublishDashboardTool().run(context, publication_id=str(imported.id), title='X', theme='light',
                                   configuration={'preset': 'external-app', 'url': 'https://dashboards.lifehealth.app/ccbu-ehs'})
    assert DashboardPublication.objects.filter(pk=imported.id, configuration__preset='external-app').count() == 0


def test_external_app_visible_to_any_viewer_without_superset(monkeypatch):
    context, client, _ = setup_publication(monkeypatch)
    result = PublishDashboardTool().run(context, title='CCBU EHS', theme='light',
                                        configuration={'preset': 'external-app', 'url': 'https://dashboards.lifehealth.app/ccbu-ehs'})
    publication = DashboardPublication.objects.get(id=result['publication_id'])
    # Another authenticated user with no chart access can still see it.
    client.get_chart.side_effect = SupersetError('Access denied', status_code=403)
    client.get_dashboard.side_effect = SupersetError('Access denied', status_code=403)
    monkeypatch.setattr(dashboards, '_client', lambda request: client)
    response = json.loads(dashboards.library(make_request(username='viewer')).content)
    assert [item['title'] for item in response['items']] == ['CCBU EHS']
    assert response['items'][0]['chart_count'] == 0
    detail = json.loads(dashboards.detail(make_request(username='viewer'), publication.id).content)
    assert detail['charts'] == []
    assert detail['can_edit'] is False


def test_external_endpoint_owner_scoped(monkeypatch):
    context, client, _ = setup_publication(monkeypatch)
    monkeypatch.setattr(dashboards, '_client', lambda request: client)
    request = make_request('POST', {'title': 'CCBU EHS', 'url': 'https://dashboards.lifehealth.app/ccbu-ehs', 'theme': 'light'}, username='ana')
    created = dashboards.external(request)
    assert created.status_code == 201
    publication_id = json.loads(created.content)['id']
    publication = DashboardPublication.objects.get(pk=publication_id)
    assert publication.owner_username == 'ana'
    # Bad URL rejected.
    bad = make_request('POST', {'title': 'X', 'url': 'https://evil.example.com/', 'theme': 'light'}, username='ana')
    assert dashboards.external(bad).status_code == 400
    # Another user cannot update or delete it.
    update = make_request('POST', {'publication_id': publication_id, 'title': 'Hijack', 'url': 'https://dashboards.lifehealth.app/ccbu-ehs', 'theme': 'light'}, username='viewer')
    assert dashboards.external(update).status_code == 404
    assert dashboards.delete(make_request('DELETE', username='viewer'), publication_id).status_code == 403
    # Owner updates and deletes.
    update = make_request('POST', {'publication_id': publication_id, 'title': 'CCBU EHS renamed', 'url': 'https://dashboards.lifehealth.app/ccbu-ehs', 'theme': 'dark'}, username='ana')
    assert dashboards.external(update).status_code == 200
    publication.refresh_from_db()
    assert publication.title == 'CCBU EHS renamed' and publication.theme == 'dark'
    assert dashboards.delete(make_request('DELETE', username='ana'), publication_id).status_code == 200
    assert DashboardPublication.objects.filter(pk=publication_id).count() == 0
