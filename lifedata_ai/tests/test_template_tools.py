from unittest.mock import Mock
from lifedata_ai.tools.geography import PrepareGeographicMapTool
from lifedata_ai.tests.test_administrative import setup_sources


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
