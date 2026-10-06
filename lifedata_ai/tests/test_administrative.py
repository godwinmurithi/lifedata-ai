"""Boundary-backed hierarchies never need admin fields in the primary dataset."""
import copy,json
from unittest.mock import Mock
import pytest
from django.test import RequestFactory
from lifedata_ai.administrative import enrich_administrative
from lifedata_ai.tools.base import ToolError,ToolContext
from lifedata_ai.middleware import SupersetIdentity
from lifedata_ai.models import DashboardPublication
from lifedata_ai.views import workspace
from lifedata_ai.tests.test_geography import polygon


def setup_sources():
    client=Mock()
    records=[{'facility_name':'A','ready':'yes','lat':0.5,'lng':0.5},
             {'facility_name':'B','ready':'no','lat':0.5,'lng':1.5},
             {'facility_name':'Missing','ready':'yes','lat':None,'lng':None}]
    areas=[{'county':'C1','subcounty':'S1','ward':'W1','geometry_geojson':polygon(0,1)},
           {'county':'C2','subcounty':'S2','ward':'W2','geometry_geojson':polygon(1,2)}]
    def chart(identifier):
        return {'id':identifier,'params':{'query_mode':'raw'},'query_context':{'datasource':{'id':identifier,'type':'table'},
            'queries':[{'columns':[],'metrics':[],'row_limit':10000,'filters':[{'col':'tenant','op':'==','val':'current'}]}]}}
    client.get_chart.side_effect=chart
    client.get_dashboard.return_value={'id':5}
    client.get_dashboard_charts.return_value=[{'id':12},{'id':13}]
    cols={12:['facility_name','ready','lat','lng'],13:['county','subcounty','ward','geometry_geojson']}
    client.get_dataset.side_effect=lambda identifier:{'columns':[{'column_name':c} for c in cols[identifier]]}
    client.chart_data.side_effect=lambda context:{'result':[{'data':copy.deepcopy(records if context['datasource']['id']==12 else areas)}]}
    levels=[{'field':f,'source_field':f,'label':f.title()} for f in ['county','subcounty','ward']]
    config={'preset':'buikwe-workspace','source_chart_id':12,'facility_chart_id':12,
        'administrative_boundaries':[{'chart_id':13,'geometry_field':'geometry_geojson','levels':levels}],
        'workspace':{'selectors':['county','subcounty','ward'],'site_field':'facility_name','country_field':'county',
            'record_columns':['facility_name','county','subcounty','ward'],
            'panels':[{'slot':'blood','title':'Ready','tabs':[{'field':'ready','label':'Ready'}]}],'workflow':[]}}
    return client,config,records


def test_spatial_join_adds_actual_parent_names_and_preserves_unknown_locations():
    client,config,rows=setup_sources()
    from lifedata_ai.geography import workspace_geography
    geo=workspace_geography(client,config,{12,13},rows,{})
    geo,levels=enrich_administrative(client,config,{12,13},rows,geo)
    assert [l['field'] for l in levels]==['county','subcounty','ward']
    assert rows[0]['ward']=='W1' and rows[1]['county']=='C2'
    assert 'county' not in rows[2]
    assert geo['counts']=={'county':2,'subcounty':2,'ward':2}
    ward=next(b for b in geo['boundaries'] if b['area_name']=='W1')
    assert ward['parents']=={'county':'C1','subcounty':'S1'}
    assert all(call.args[0]['queries'][0]['filters']==[{'col':'tenant','op':'==','val':'current'}] for call in client.chart_data.call_args_list)


def test_shared_edges_do_not_invent_child_areas():
    client,config,rows=setup_sources()
    rows=[{'facility_name':'Border','lat':0.5,'lng':1.0}]
    geo={'facilities':[], 'all_facilities':copy.deepcopy(rows), 'boundaries':[], 'counts':{}, 'notes':[], 'truncated':False,'source_chart_ids':[]}
    result,_=enrich_administrative(client,config,{13},rows,geo)
    assert 'county' not in rows[0] and 'ward' not in rows[0]
    assert any('could not be uniquely matched' in n for n in result['notes'])


def test_foreign_reference_and_nonexistent_columns_are_denied():
    client,config,rows=setup_sources()
    with pytest.raises(ToolError):enrich_administrative(client,config,{12},rows,None)
    client.chart_data.assert_not_called()
    config['administrative_boundaries'][0]['levels'][0]['source_field']='foreign_column'
    with pytest.raises(ToolError):enrich_administrative(client,config,{12,13},rows,None)
    client.chart_data.assert_not_called()


@pytest.mark.django_db
def test_workspace_filters_and_cascades_on_enriched_fields(monkeypatch):
    client,config,_=setup_sources()
    p=DashboardPublication.objects.create(superset_dashboard_id=5,owner_username='ana',title='Client',configuration=config)
    monkeypatch.setattr(workspace,'_client',lambda request:client)
    request=RequestFactory().post('/workspace/',data=json.dumps({'criteria':{'county':['C1']}}),content_type='application/json')
    request.superset_identity=SupersetIdentity(username='ana')
    response=workspace.data(request,p.id)
    assert response.status_code==200
    data=json.loads(response.content)
    assert data['matches']==1 and data['rows'][0]['facility_name']=='A'
    assert data['options']['county']==['C1','C2']
    assert data['options']['subcounty']==['S1'] and data['options']['ward']==['W1']
    assert len(data['geography']['facilities'])==1
    assert all(b.get('parents',{}).get('county','C1')=='C1' for b in data['geography']['boundaries'])
    primary=client.chart_data.call_args_list[0].args[0]
    assert 'county' not in primary['queries'][0]['columns']


def test_discovery_searches_outside_primary_dataset_and_keeps_country_evidence():
    from lifedata_ai.tools.geography import DiscoverAdministrativeBoundariesTool
    client=Mock()
    client.list_datasets.side_effect=lambda term:[{'id':13,'table_name':'National reference'},{'id':14,'table_name':'Other country'}]
    client.get_dataset.side_effect=lambda identifier:{'table_name':'Reference','database':{'id':3},
        'columns':[{'column_name':'adm0_name'},{'column_name':'district'},{'column_name':'geometry'}]}
    client.chart_data.side_effect=lambda query:{'result':[{'data':[{'adm0_name':'Uganda' if query['datasource']['id']==13 else 'Congo'}]}]}
    context=ToolContext(superset_session_cookie='fake',identity=SupersetIdentity(username='ana'));context._client=client
    result=DiscoverAdministrativeBoundariesTool().run(context,'Uganda')
    assert len(result['candidates'])==2
    assert result['candidates'][1]['geographic_sample'][0]['adm0_name']=='Congo'
    assert 'Verify country coverage' in result['message']
    assert {c.args[0] for c in client.list_datasets.call_args_list}>={'Uganda','county','ward','parish'}
    from lifedata_ai.tools.registry import build_registry
    assert 'discover_administrative_boundaries' in build_registry().names()
