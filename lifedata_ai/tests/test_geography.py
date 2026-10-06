"""Workspace maps reuse permitted geography, recorded classes and actual coordinates."""
import copy
from unittest.mock import Mock
import pytest
from lifedata_ai.geography import source_rows, workspace_geography
from lifedata_ai.tools.base import ToolError


def polygon(left, right):
    return {'type':'Polygon','coordinates':[[[left,0],[right,0],[right,1],[left,1],[left,0]]]}


def sources():
    client = Mock()
    def chart(identifier):
        return {'id':identifier,'params':{'query_mode':'raw'},'query_context':{
            'datasource':{'id':identifier,'type':'table'},'queries':[{
                'columns':['facility_name'] if identifier==101 else ['geometry_geojson'],
                'row_limit':10000,'filters':[{'col':'district','op':'IN','val':['Current client']}]}]}}
    client.get_chart.side_effect=chart
    columns={101:['facility_name','lat','lng','facility_level','beds','county','subcounty'],
             102:['county','subcounty','geometry_geojson']}
    client.get_dataset.side_effect=lambda identifier:{'columns':[{'column_name':c} for c in columns[identifier]]}
    facilities=[{'facility_name':'Site A','lat':0.25,'lng':0.5,'facility_level':'HCII','beds':0,'county':'C','subcounty':'S1'},
        {'facility_name':'Site B','lat':0.5,'lng':1.5,'facility_level':'Hospital','beds':120,'county':'C','subcounty':'S2'},
        {'facility_name':'Site A','lat':0.25,'lng':0.5,'facility_level':'HCII','beds':0,'county':'C','subcounty':'S1'},
        {'facility_name':'Missing point','lat':None,'lng':0,'facility_level':None,'beds':None}]
    areas=[{'county':'C','subcounty':'S1','geometry_geojson':polygon(0,1)},
           {'county':'C','subcounty':'S2','geometry_geojson':polygon(1,2)}]
    def run(context):
        identifier=context['datasource']['id']
        return {'result':[{'data':copy.deepcopy(facilities if identifier==101 else areas)}]}
    client.chart_data.side_effect=run
    config={'facility_chart_id':101,'boundary_chart_id':102,'workspace':{'site_field':'site'}}
    return client,config


def test_geography_uses_real_points_and_dissolves_county_without_changing_styles():
    from django.contrib.gis.geos import GEOSGeometry
    import json
    client,config=sources()
    result=workspace_geography(client,config,{101,102},[{'site':'SITE A'}],{})
    assert len(result['facilities'])==1 and len(result['all_facilities'])==2
    assert result['facilities'][0]['lat']==0.25 and result['facilities'][0]['lng']==0.5
    assert result['facilities'][0]['beds']==0
    assert result['all_facilities'][1]['facility_level']=='Hospital'
    assert result['all_facilities'][1]['beds']==120
    assert result['counts']=={'county':1,'subcounty':2}
    county=next(b for b in result['boundaries'] if b['area_level']=='county')
    assert GEOSGeometry(json.dumps(county['geometry_geojson'])).area==2
    assert result['notes']==['1 geographic records have missing or invalid coordinates.']
    for call in client.chart_data.call_args_list:
        assert call.args[0]['queries'][0]['filters']==[{'col':'district','op':'IN','val':['Current client']}]
    assert 'beds' in client.chart_data.call_args_list[0].args[0]['queries'][0]['columns']


def test_geography_counts_unmatched_names_and_supports_area_filters():
    client,config=sources()
    result=workspace_geography(client,config,{101,102},[{'site':'Site A'},{'site':'Site B'},{'site':'New site'}],{'subcounty':['s2']})
    assert [f['facility_name'] for f in result['facilities']]==['Site B']
    assert result['unmatched_facilities']==1
    assert any('no matching location' in n for n in result['notes'])
    result=workspace_geography(client,config,{101,102},[],{})
    assert result['facilities']==[]


def test_geography_denies_unrelated_chart_before_any_data_read():
    client,_=sources()
    with pytest.raises(ToolError) as exc:
        source_rows(client,999,{101,102},['lat','lng'])
    assert exc.value.code=='forbidden'
    client.get_chart.assert_not_called()
    client.chart_data.assert_not_called()


def test_geography_reports_saved_source_limit():
    client,config=sources()
    original=client.get_chart.side_effect
    def capped(identifier):
        chart=original(identifier);chart['query_context']['queries'][0]['row_limit']=2;return chart
    client.get_chart.side_effect=capped
    result=workspace_geography(client,config,{101,102},[{'site':'Site A'}],{})
    assert result['truncated']
    assert any('saved row limit' in n for n in result['notes'])
