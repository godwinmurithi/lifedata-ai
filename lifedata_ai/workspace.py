"""Data-independent configuration for the exact Buikwe workspace."""
SLOTS = ['blood', 'donor', 'liver', 'screening', 'pressure', 'vaccines',
         'hematology', 'disability', 'sexual', 'renal', 'systemic']
WORKSPACE_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'layout_mode': {'enum': ['compact', 'expanded']},
        'display_title': {'type': 'string', 'maxLength': 80},
        'record_label': {'type': 'string', 'minLength': 1, 'maxLength': 40},
        'entity_label': {'type': 'string', 'minLength': 1, 'maxLength': 40},
        'geography_label': {'type': 'string', 'minLength': 1, 'maxLength': 40},
        'selectors': {'type': 'array', 'maxItems': 8, 'items': {'type': 'string'}},
        'county_field': {'type': 'string'}, 'subcounty_field': {'type': 'string'},
        'site_field': {'type': 'string'}, 'country_field': {'type': 'string'},
        'record_columns': {'type': 'array', 'minItems': 1, 'maxItems': 30, 'items': {'type': 'string'}},
        'panels': {'type': 'array', 'minItems': 1, 'maxItems': 11, 'items': {
            'type': 'object', 'additionalProperties': False,
            'properties': {'slot': {'enum': SLOTS}, 'title': {'type': 'string', 'minLength': 1},
                'tabs': {'type': 'array', 'minItems': 1, 'maxItems': 12, 'items': {
                    'type': 'object', 'additionalProperties': False,
                    'properties': {'field': {'type': 'string'}, 'label': {'type': 'string'},
                                   'chart_id': {'type': 'integer', 'minimum': 1},
                                   'renderer': {'enum': ['donut', 'bar', 'line', 'kpi']},
                                   'measure': {'type': 'string'},
                                   'aggregation': {'enum': ['sum', 'avg']}},
                    'required': ['field', 'label']}}}, 'required': ['slot', 'title', 'tabs']}},
        'workflow': {'type': 'array', 'maxItems': 5, 'items': {
            'type': 'object', 'additionalProperties': False,
            'properties': {'field': {'type': 'string'}, 'value': {'type': 'string'}, 'label': {'type': 'string'}},
            'required': ['field', 'value', 'label']}},
        'summary_chart_ids': {'type': 'array', 'items': {'type': 'integer'}},
        'country_chart_id': {'type': 'integer'},
        'detail_chart_ids': {'type': 'array', 'items': {'type': 'integer'}},
    }, 'required': ['selectors', 'site_field', 'country_field', 'record_columns', 'panels', 'workflow'],
}

def validate_workspace(workspace, chart_ids):
    import jsonschema
    from lifedata_ai.tools.base import ToolError
    try:
        jsonschema.validate(workspace, WORKSPACE_SCHEMA)
    except jsonschema.ValidationError as exc:
        raise ToolError('Invalid Buikwe workspace configuration: ' + exc.message, code='invalid_configuration') from exc
    slots = [p['slot'] for p in workspace['panels']]
    if len(slots) != len(set(slots)):
        raise ToolError('Workspace panels need distinct positions.', code='invalid_configuration')
    references = workspace.get('summary_chart_ids', []) + workspace.get('detail_chart_ids', [])
    if workspace.get('country_chart_id'):
        references.append(workspace['country_chart_id'])
    references += [t['chart_id'] for p in workspace['panels'] for t in p['tabs'] if t.get('chart_id')]
    if any(identifier not in chart_ids for identifier in references):
        raise ToolError('Workspace chart references must belong to this dashboard.', code='invalid_configuration')
