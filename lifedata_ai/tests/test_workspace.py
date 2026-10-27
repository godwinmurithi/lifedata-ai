"""The shared Buikwe screen uses only current-client records and permissions."""
import json
from unittest.mock import Mock
import pytest
from django.test import RequestFactory
from lifedata_ai.middleware import SupersetIdentity
from lifedata_ai.models import DashboardPublication
from lifedata_ai.superset_client import SupersetError
from lifedata_ai.views import workspace

pytestmark = pytest.mark.django_db

@pytest.fixture
def source(monkeypatch):
    settings = {'selectors':['country'], 'site_field':'site_name', 'country_field':'country',
        'record_columns':['site_name','country'],
        'panels':[{'slot':'blood','title':'Readiness','tabs':[{'field':'ready','label':'Ready'}, {'field':'domains','label':'Domains'}]}],
        'workflow':[{'field':'ready','value':'yes','label':'Ready'}]}
    publication = DashboardPublication.objects.create(superset_dashboard_id=50, owner_username='ana',title='Client workspace',
        configuration={'preset':'buikwe-workspace','source_chart_id':505,'workspace':settings})
    client = Mock()
    client.get_dashboard.return_value={'id':50}
    client.get_dashboard_charts.return_value=[{'id':505}]
    client.get_chart.return_value={'id':505,'params':{'query_mode':'raw'},'query_context':{'datasource':{'id':210,'type':'table'},
        'queries':[{'columns':['site_name'], 'row_limit':10000, 'filters':[{'col':'client_id','op':'==','val':'this-client'}]}]}}
    client.get_dataset.return_value={'columns':[{'column_name':field} for field in ['site_name','country','ready','domains']]}
    client.chart_data.return_value={'result':[{'data':[
        {'site_name':'A','country':'ghana','ready':'yes','domains':'["clinical", "lab", "clinical"]'},
        {'site_name':'B','country':'mali','ready':'no','domains':'["clinical"]'},
        {'site_name':'C','country':'ghana','ready':None,'domains':None}]}]}
    monkeypatch.setattr(workspace,'_client',lambda request:client)
    return publication,client


def request(criteria=None):
    req=RequestFactory().post('/workspace/',data=json.dumps({'criteria':criteria or {}}),content_type='application/json')
    req.superset_identity=SupersetIdentity(username='ana')
    return req


def test_workspace_keeps_source_filters_and_counts_multi_answers_once_per_record(source):
    publication,client=source
    response=workspace.data(request(),publication.id);assert response.status_code==200
    data=json.loads(response.content)
    assert (data['matches'],data['sites'],data['countries'])==(3,3,2)
    assert {'domains':'clinical','people':2} in data['breakdowns']['domains']
    assert data['stages']==[{'label':'Current assessments','count':3},{'label':'Ready','count':1}]
    query=client.chart_data.call_args.args[0]
    assert query['datasource']['id']==210
    assert query['queries'][0]['filters']==[{'col':'client_id','op':'==','val':'this-client'}]
    assert set(query['queries'][0]['columns'])=={'site_name','country','ready','domains'}


def test_workspace_filters_every_panel_and_details_from_same_cohort(source):
    publication,_=source
    response=workspace.data(request({'country':['mali']}),publication.id)
    data=json.loads(response.content)
    assert data['matches']==1 and data['rows'][0]['site_name']=='B'
    assert data['breakdowns']['ready']==[{'ready':'no','people':1}]
    assert data['options']['country']==['ghana','mali']


def test_filter_reuses_source_but_refresh_and_session_changes_reload(source):
    publication, client = source
    first = request()
    first.superset_session_cookie = 'session-a'
    assert workspace.data(first, publication.id).status_code == 200
    filtered = request({'country': ['mali']})
    filtered.superset_session_cookie = 'session-a'
    assert json.loads(workspace.data(filtered, publication.id).content)['matches'] == 1
    assert client.chart_data.call_count == 1
    fresh = RequestFactory().post('/workspace/', data=json.dumps({'refresh': True}), content_type='application/json')
    fresh.superset_identity = first.superset_identity
    fresh.superset_session_cookie = 'session-a'
    assert workspace.data(fresh, publication.id).status_code == 200
    assert client.chart_data.call_count == 2
    filtered.superset_session_cookie = 'session-b'
    assert workspace.data(filtered, publication.id).status_code == 200
    assert client.chart_data.call_count == 3
    client.get_dashboard.side_effect = SupersetError('Denied', status_code=403)
    assert workspace.data(first, publication.id).status_code == 403
    assert client.chart_data.call_count == 3


def test_workspace_requires_source_dashboard_and_chart_permission(source):
    publication,client=source
    client.get_dashboard_charts.return_value=[{'id':999}]
    assert workspace.data(request(),publication.id).status_code==403
    client.chart_data.assert_not_called()
    client.get_dashboard.side_effect=SupersetError('Denied',status_code=403)
    assert workspace.data(request(),publication.id).status_code==403


def test_geometry_reuse_keeps_area_metadata_and_other_viewers_get_coordinates(source, monkeypatch):
    publication, client = source
    boundary = {'field': 'country', 'area_name': 'ghana', 'parents': {},
                'geometry_geojson': {'type': 'Polygon', 'coordinates': [[[0, 0], [1, 0], [0, 0]]]}}
    geography = {'boundaries': [boundary], 'all_facilities': [], 'facilities': [], 'counts': {}}
    monkeypatch.setattr(workspace, 'enrich_administrative', lambda *args: (geography, []))
    first = json.loads(workspace.data(request(), publication.id).content)
    assert first['geography']['boundaries'][0]['geometry_geojson']
    req = RequestFactory().post('/workspace/', data=json.dumps({'criteria': {'country': ['ghana']},
        'geometry_version': first['geometry_version']}), content_type='application/json')
    req.superset_identity = SupersetIdentity(username='ana')
    second = json.loads(workspace.data(req, publication.id).content)
    assert second['geography']['boundaries'][0]['area_name'] == 'ghana'
    assert 'geometry_geojson' not in second['geography']['boundaries'][0]
    assert second['matches'] == 2
    assert client.chart_data.call_count == 1
    third = json.loads(workspace.data(request(), publication.id).content)
    assert third['geography']['boundaries'][0]['geometry_geojson']


def test_workspace_rejects_fields_outside_the_current_source_dataset(source):
    publication,client=source
    publication.configuration['workspace']['record_columns'].append('another_clients_field')
    publication.save()
    assert workspace.data(request(),publication.id).status_code==400
    client.chart_data.assert_not_called()


def test_workspace_marks_capped_results_instead_of_claiming_all_records(source):
    publication,client=source
    client.chart_data.return_value={'result':[{'data':[{'site_name':'A','country':'ghana','ready':'yes','domains':None}]*10000}]}
    response=workspace.data(request(),publication.id)
    assert json.loads(response.content)['truncated'] is True


def test_numeric_workspace_without_site_or_geography(source):
    publication, client = source
    publication.configuration['workspace'] = {
        'selectors': ['category'], 'site_field': '', 'country_field': '',
        'record_label': 'Orders', 'record_columns': ['category', 'amount'],
        'panels': [{'slot': 'blood', 'title': 'Revenue', 'tabs': [
            {'field': 'category', 'label': 'Revenue', 'renderer': 'bar', 'measure': 'amount'},
            {'field': 'category', 'label': 'Average order', 'renderer': 'bar', 'measure': 'amount', 'aggregation': 'avg'}]}],
        'workflow': []}
    publication.save()
    client.get_dataset.return_value = {'columns': [{'column_name': f} for f in ['category', 'amount']]}
    client.chart_data.return_value = {'result': [{'data': [
        {'category': 'A', 'amount': 10}, {'category': 'A', 'amount': '20'},
        {'category': 'B', 'amount': None}, {'category': 'A', 'amount': 'NaN'}]}]}
    response = workspace.data(request(), publication.id)
    assert response.status_code == 200
    data = json.loads(response.content)
    assert data['series']['category:amount:sum'] == [{'label': 'A', 'value': 30}]
    assert data['series']['category:amount:avg'] == [{'label': 'A', 'value': 15}]
    assert data['sites'] == data['countries'] == 0
    assert data['stages'] == [{'label': 'Current orders', 'count': 4}]
    assert client.chart_data.call_args.args[0]['queries'][0]['columns'] == ['category', 'amount']


def style_context(client, username='ana'):
    from lifedata_ai.tools.base import ToolContext
    ctx = ToolContext(superset_session_cookie='dummy', identity=SupersetIdentity(username=username))
    ctx._client = client
    return ctx


def test_single_chart_style_patch_preserves_page_and_other_charts(source):
    import copy
    from lifedata_ai.tools.publications import EditDashboardChartStyleTool
    publication, client = source
    publication.configuration['design'] = {'baseline': 'vima-buikwe-v1', 'colors': {'yes': '#13a58a'},
        'chart_styles': {'field:ready': {'colors': {'no': '#ff0000'}}}}
    publication.save()
    original = copy.deepcopy(publication.configuration)
    tool = EditDashboardChartStyleTool()
    targets = tool.run(style_context(client), 50)
    assert {t['target'] for t in targets['targets']} == {'gauge', 'field:ready', 'field:domains'}
    result = tool.run(style_context(client), 50, target='gauge', style={'color': '#16a34a'})
    assert result['updated_target'] == 'gauge'
    publication.refresh_from_db()
    assert publication.configuration['workspace'] == original['workspace']
    assert publication.configuration['source_chart_id'] == 505
    assert publication.configuration['design']['chart_styles']['gauge'] == {'color': '#16a34a'}
    assert publication.configuration['design']['chart_styles']['field:ready'] == original['design']['chart_styles']['field:ready']
    assert publication.configuration['design']['colors'] == original['design']['colors']
    tool.run(style_context(client), 50, target='field:ready', style={'colors': {'yes': '#008800'}})
    publication.refresh_from_db()
    assert publication.configuration['design']['chart_styles']['field:ready']['colors'] == {'yes': '#008800', 'no': '#ff0000'}
    tool.run(style_context(client), 50, target='gauge', reset=True)
    publication.refresh_from_db()
    assert 'gauge' not in publication.configuration['design']['chart_styles']
    assert 'field:ready' in publication.configuration['design']['chart_styles']
    client.chart_data.assert_not_called()


def test_chart_style_rejects_foreign_owner_unknown_target_and_invalid_color(source):
    import copy
    from lifedata_ai.tools.publications import EditDashboardChartStyleTool
    from lifedata_ai.tools.base import ToolError
    publication, client = source
    original = copy.deepcopy(publication.configuration)
    tool = EditDashboardChartStyleTool()
    with pytest.raises(ToolError):
        tool.run(style_context(client, 'someone-else'), 50, target='gauge', style={'color': '#16a34a'})
    with pytest.raises(ToolError):
        tool.run(style_context(client), 50, target='chart:999', style={'color': '#16a34a'})
    with pytest.raises(ToolError):
        tool.run(style_context(client), 50, target='gauge', style={'color': 'green'})
    publication.refresh_from_db()
    assert publication.configuration == original


def test_facility_template_has_individual_service_and_kpi_targets(source):
    from lifedata_ai.tools.publications import EditDashboardChartStyleTool
    publication, client = source
    publication.configuration = {'preset': 'facility-network', 'source_chart_id': 505, 'network_chart_id': 505,
        'design': {'baseline': 'vima-buikwe-v1', 'colors': {'yes': '#16a34a'}}}
    publication.save()
    tool = EditDashboardChartStyleTool()
    result = tool.run(style_context(client), 50)
    assert {'field:labaratory', 'field:kpi-0', 'field:ownership'} <= {t['target'] for t in result['targets']}
    tool.run(style_context(client), 50, target='field:labaratory', style={'colors': {'yes': '#2563eb'}})
    publication.refresh_from_db()
    assert publication.configuration['design']['colors']['yes'] == '#16a34a'
    assert publication.configuration['design']['chart_styles'] == {'field:labaratory': {'colors': {'yes': '#2563eb'}}}
    assert publication.configuration['preset'] == 'facility-network'
    client.chart_data.assert_not_called()


def test_native_css_is_refused_on_custom_surface_before_tool_runs(source):
    from lifedata_ai.tools.registry import execute, ToolRegistry
    from lifedata_ai.tools.base import Tool, ToolMode
    class NativeStyle(Tool):
        name = 'set_dashboard_css'
        mode = ToolMode.READ
        description = 'test'
        input_schema = {'type': 'object'}
        def run(self, context, **arguments):
            raise AssertionError('Native Superset tool must never run')
    _, client = source
    ctx = style_context(client)
    ctx.page_context = {'page_type': 'dashboard', 'dashboard_id': 50, 'dashboard_surface': 'custom'}
    registry = ToolRegistry();registry.register(NativeStyle())
    result = execute('set_dashboard_css', {}, ctx, registry)
    assert not result.ok
    assert result.content['error'] == 'custom_dashboard_style_required'
