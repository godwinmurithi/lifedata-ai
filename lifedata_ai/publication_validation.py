"""Fail early when a requested live visual cannot be rendered faithfully."""
from lifedata_ai.composition import viz
from lifedata_ai.tools.base import ToolError


def validate_renderers(config, charts):
    if config.get('preset') not in {'composed-dashboard', 'reference-template'}:
        return
    encodings = config.get('composition', {}).get('encodings', {})
    bound_encodings = {}
    for binding in config.get('bindings', {}).values():
        for identifier in binding.get('chart_ids', []):
            if binding.get('geography'):
                bound_encodings[identifier] = {'chart_type': 'map'}
            elif binding.get('encoding'):
                bound_encodings[identifier] = binding['encoding']
    unsupported = []
    for chart in charts:
        identifier = int(chart['id'])
        encoding = bound_encodings.get(identifier) or encodings.get(str(identifier), {})
        if encoding.get('chart_type'):
            continue
        kind = viz(chart)
        if (kind.startswith('big_number') or kind in {'table', 'pie', 'scatter', 'bar', 'line', 'area', 'dist_bar'}
                or 'timeseries_line' in kind or 'timeseries_bar' in kind or 'timeseries_area' in kind):
            continue
        # Geographic reference charts are helper sources, not visible visuals.
        helper_ids = {source['chart_id'] for binding in config.get('bindings', {}).values()
                      for source in binding.get('geography', {}).get('administrative_boundaries', [])}
        visible_ids = {i for binding in config.get('bindings', {}).values() for i in binding.get('chart_ids', [])}
        if identifier in helper_ids and identifier not in visible_ids:
            continue
        unsupported.append({'chart_id': identifier, 'viz_type': kind})
    if unsupported:
        raise ToolError(
            'These source visuals need an explicit supported encoding or an executable raw map source. '
            'A table fallback does not satisfy the requested visualization.',
            code='unsupported_renderer', details={'charts': unsupported,
                'next_step': 'Inspect get_dashboard_capabilities and provide current-source encodings or supported charts.'})
