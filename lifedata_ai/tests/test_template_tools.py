from hashlib import sha256
from unittest.mock import Mock
from lifedata_ai.tools.templates import GetDashboardTemplateTool
from lifedata_ai.tools.geography import PrepareGeographicMapTool
from lifedata_ai.tests.test_administrative import setup_sources


def test_original_reference_is_returned_without_rewriting_or_live_data_claims():
    result = GetDashboardTemplateTool().run(None, 'sincep-hdss')
    assert result['sha256'] == '37f8fb2337afbaae241b3e7e3ca7e030606f3da48f6bbb709b9abbdb16cc0863'
    assert sha256(result['html'].encode()).hexdigest() == result['sha256']
    assert result['data_status'] == 'illustrative_reference_only'
    assert result['executable_publication_preset'] == 'reference-template'
    assert 'residents' in result['slots']
    assert './support.js (x-dc/sc-for/sc-if runtime)' in result['runtime_dependencies']
    assert all('ECharts' not in dependency and 'Leaflet' not in dependency for dependency in result['runtime_dependencies'])


def test_map_dry_run_uses_real_join_preserves_filters_and_returns_no_records():
    client, config, _ = setup_sources()
    context = Mock()
    context.superset_client.return_value = client
    result = PrepareGeographicMapTool().run(context, 12, 'facility_name', config['administrative_boundaries'])
    assert result['records_checked'] == 3
    assert result['join_diagnostics']['matched_records'] == 2
    assert result['records_missing_level_names'] == 1
    assert result['coverage']['county']['named_records_after_join'] == 2
    assert not result['truncated']
    assert 'rows' not in result and 'html' not in result
    for call in client.chart_data.call_args_list:
        assert call.args[0]['queries'][0]['filters'] == [{'col':'tenant','op':'==','val':'current'}]
