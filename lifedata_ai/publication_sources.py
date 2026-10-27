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
    for identifier in configuration.get('chart_ids',[]):
        add(identifier)
    for binding in configuration.get('bindings', {}).values():
        for identifier in binding.get('chart_ids', []):
            add(identifier)
        geography = binding.get('geography', {})
        add(geography.get('source_chart_id'))
        for source in geography.get('administrative_boundaries', []):
            add(source.get('chart_id'))
    for source in configuration.get('administrative_boundaries', []):
        add(source.get('chart_id'))
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
    if (publication.configuration or {}).get('preset') in ('hdss-site', 'hdss-master'):
        # Template pages bind a registered dataset, not charts: authorise the dataset with the viewer's session.
        from lifedata_ai.hdss.service import authorise
        authorise(client, publication)
        return []
    if publication.superset_dashboard_id is not None:
        if not client.get_dashboard(publication.superset_dashboard_id):
            raise SupersetError('Dashboard not found.', status_code=404)
        charts = client.get_dashboard_charts(publication.superset_dashboard_id)
        members = {c['id'] for c in charts}
        for source in publication.configuration.get('administrative_boundaries', []):
            if source['chart_id'] not in members:
                charts.append(client.get_chart(source['chart_id']))
                members.add(source['chart_id'])
        return charts
    # Never infer membership from a requested chart ID. Saved references are the
    # allow-list; every read still authorises sources using the viewer's cookie.
    ids = [item['chart_id'] for item in publication.layout]
    if not ids:
        raise SupersetError('Publication has no sources.', status_code=404)
    return [client.get_chart(identifier) for identifier in ids]
