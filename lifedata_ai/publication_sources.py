"""Publication source membership and viewer permissions without a native dashboard."""
from lifedata_ai.superset_client import SupersetError


def configuration_chart_ids(configuration):
    ids = []
    def add(value):
        if type(value) is int and value > 0 and value not in ids:
            ids.append(value)
    for key in ('source_chart_id', 'facility_chart_id', 'boundary_chart_id',
                'network_chart_id', 'summary_chart_id'):
        add(configuration.get(key))
    workspace = configuration.get('workspace') or {}
    for key in ('summary_chart_ids', 'detail_chart_ids'):
        for identifier in workspace.get(key, []):
            add(identifier)
    add(workspace.get('country_chart_id'))
    for panel in workspace.get('panels', []):
        for tab in panel.get('tabs', []):
            add(tab.get('chart_id'))
    return ids


def publication_charts(client, publication):
    if publication.superset_dashboard_id is not None:
        if not client.get_dashboard(publication.superset_dashboard_id):
            raise SupersetError('Dashboard not found.', status_code=404)
        return client.get_dashboard_charts(publication.superset_dashboard_id)
    # Never infer membership from a requested chart ID. Saved references are the
    # allow-list; every read still authorises sources using the viewer's cookie.
    ids = [item['chart_id'] for item in publication.layout]
    if not ids:
        raise SupersetError('Publication has no sources.', status_code=404)
    return [client.get_chart(identifier) for identifier in ids]
