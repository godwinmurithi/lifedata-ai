"""Discover the actual publication and rendering contracts before planning."""
from lifedata_ai.tools.base import Tool, ToolMode


class GetDashboardCapabilitiesTool(Tool):
    name = 'get_dashboard_capabilities'
    mode = ToolMode.READ
    description = ('Discover supported dashboard renderers, exact template bindings, shared chart stacks, '
                   'filters and quality lifecycle before creating a dashboard. Use these executable contracts '
                   'instead of guessing whether a Superset visualization is supported by the custom viewer.')
    input_schema = {'type': 'object', 'additionalProperties': False, 'properties': {}, 'required': []}

    def run(self, context):
        from lifedata_ai.composition import RENDERER_CAPABILITIES, COMPOSITION_SCHEMA
        from lifedata_ai.dashboard_design import DESIGN_SCHEMA
        from lifedata_ai.tools.quality import SPECIFICATION_SCHEMA
        return {
            'renderers': RENDERER_CAPABILITIES,
            'composition_schema': COMPOSITION_SCHEMA,
            'chart_stack_schema': DESIGN_SCHEMA['properties']['groups'],
            'brief_schema': SPECIFICATION_SCHEMA,
            'presets': ['buikwe-workspace', 'composed-dashboard'],
            'workflow': ['profile_dataset', 'plan_dashboard', 'get_dashboard_template when requested',
                         'create or reuse executable current-client charts', 'prepare_geographic_map when needed',
                         'publish_dashboard with specification', 'inspect_dashboard',
                         'repair findings and inspect again', 'complete_dashboard_build'],
            'rules': ['An unsupported visualization is not a successful dashboard chart.',
                      'Use explicit dimensions, measures, series, aggregation, units and formats where needed.',
                      'Stack related charts in one panel; use view tabs for detail and readable viewport fit.',
                      'There is no registered preset for an attached design reference (screenshot or HTML page): '
                      'inspect the attachment directly and reproduce its card order/sizing via publish_dashboard\'s '
                      'own layout parameter against composed-dashboard charts, never by inventing or reaching for '
                      'an unrelated preset.',
                      'Saved revisions and query definitions invalidate old quality evidence when changed.'],
        }
