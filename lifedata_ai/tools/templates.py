"""Inspect preserved references without treating illustrative values as client data."""
from lifedata_ai.tools.base import Tool, ToolMode


class GetDashboardTemplateTool(Tool):
    name = 'get_dashboard_template'
    mode = ToolMode.READ
    description = ('Inspect a registered dashboard reference before planning. '
                   'Use its design as a quality reference; inspect current permitted data separately.')
    input_schema = {'type': 'object', 'additionalProperties': False,
                    'properties': {'template': {'enum': ['buikwe']}}, 'required': ['template']}

    def run(self, context, template):
        from lifedata_ai.workspace import WORKSPACE_SCHEMA
        return {'template': template, 'preset': 'buikwe-workspace', 'workspace_schema': WORKSPACE_SCHEMA,
                'instructions': 'Bind current-client fields and use subject tabs to stack related charts. '
                                'Preserve the compact five-column, three-row viewport layout.'}
