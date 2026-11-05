"""Publication source membership and viewer permissions without a native dashboard."""
from urllib.parse import urlsplit, urlunsplit

from lifedata_ai.superset_client import SupersetError
from lifedata_ai.tools.base import ToolError

#: Hosts allowed for external-app publications (embedded in the custom UI via iframe).
EXTERNAL_URL_ALLOWED_HOSTS = frozenset({'dashboards.lifehealth.app'})


def is_external_app(publication) -> bool:
    return ((publication.configuration or {}).get('preset') == 'external-app')


def validate_external_url(url) -> str:
    """Return a normalised https URL on an approved host, or raise ToolError."""
    try:
        parts = urlsplit(str(url))
    except ValueError:
        raise ToolError('External app URLs must be valid https addresses.', code='invalid_configuration') from None
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
        raise ToolError('External app URLs must be https addresses without credentials.', code='invalid_configuration')
    if parts.hostname not in EXTERNAL_URL_ALLOWED_HOSTS:
        raise ToolError('External app host must be one of: ' + ', '.join(sorted(EXTERNAL_URL_ALLOWED_HOSTS)) + '.',
                        code='invalid_configuration')
    return urlunsplit((parts.scheme, parts.netloc, parts.path or '/', parts.query, parts.fragment))


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
    if is_external_app(publication):
        # External applications embed a URL, not Superset charts; visible to any
        # authenticated user and authorised without a source-chart lookup.
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
