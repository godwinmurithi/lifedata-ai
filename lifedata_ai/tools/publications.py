"""Publish custom dashboard pages without modifying Superset's own UI."""
from __future__ import annotations

import json
from typing import Any

from lifedata_ai.dashboard_design import DESIGN_SCHEMA, validate_design
from lifedata_ai.workspace import WORKSPACE_SCHEMA, validate_workspace
from lifedata_ai.models import DashboardPublication
from lifedata_ai.tools.base import Tool, ToolContext, ToolError, ToolMode


def layout_chart_ids(dashboard: dict[str, Any]) -> set[int]:
    """Reachable visual chart references, independent of Superset's relation list."""
    position = dashboard.get('position_json') or {}
    if isinstance(position, str):
        try:
            position = json.loads(position)
        except ValueError:
            return set()
    if not isinstance(position, dict):
        return set()
    found: set[int] = set()
    visited: set[str] = set()
    def visit(key):
        if not isinstance(key, str) or key in visited:
            return
        visited.add(key)
        node = position.get(key)
        if not isinstance(node, dict):
            return
        meta = node.get('meta') or {}
        identifier = meta.get('chartId') if isinstance(meta, dict) else None
        if node.get('type') == 'CHART' and type(identifier) is int:
            found.add(identifier)
        children = node.get('children') or []
        if isinstance(children, list):
            for child in children:
                visit(child)
    visit('ROOT_ID')
    return found


def chart_order(dashboard: dict[str, Any], charts: list[dict[str, Any]]) -> list[int]:
    """Follow the dashboard's layout tree, then include any remaining charts."""
    position = dashboard.get('position_json') or {}
    if isinstance(position, str):
        try:
            position = json.loads(position)
        except ValueError:
            position = {}
    if not isinstance(position, dict):
        position = {}
    available = {int(chart['id']) for chart in charts}
    order: list[int] = []
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visited:
            return
        visited.add(node_id)
        node = position.get(node_id, {})
        if not isinstance(node, dict):
            return
        chart_id = (node.get('meta') or {}).get('chartId')
        if chart_id in available and chart_id not in order:
            order.append(chart_id)
        for child in node.get('children') or []:
            visit(child)

    visit('ROOT_ID')
    return order + [chart_id for chart_id in sorted(available) if chart_id not in order]


def validate_layout(layout: Any, chart_ids: set[int]) -> list[dict[str, int]]:
    """Require every source chart once, with supported custom-page dimensions."""
    if not isinstance(layout, list) or len(layout) != len(chart_ids):
        raise ValueError('Include every source chart exactly once.')
    seen = set()
    for item in layout:
        if not isinstance(item, dict) or set(item) != {'chart_id', 'span', 'height'}:
            raise ValueError('Invalid layout item.')
        chart_id, span, height = item['chart_id'], item['span'], item['height']
        if type(chart_id) is not int or chart_id not in chart_ids or chart_id in seen:
            raise ValueError('Unknown or repeated chart.')
        if type(span) is not int or span not in (4, 6, 12):
            raise ValueError('Width must be 4, 6 or 12 columns.')
        if type(height) is not int or not 200 <= height <= 800:
            raise ValueError('Height must be between 200 and 800 pixels.')
        seen.add(chart_id)
    return layout


class PublishDashboardTool(Tool):
    """Persist publication settings in LifeData; Superset remains the data engine."""

    name = 'publish_dashboard'
    mode = ToolMode.READ
    description = (
        'Publish directly to the custom UI by default WITHOUT creating a Superset dashboard. '
        'Omit dashboard_id for custom-only publishing; configuration references accessible saved raw source charts. '
        'Reuse existing raw charts and render variables from them; do not create a native chart per panel. '
        'For updates pass publication_id, not a new dashboard. New custom-only publications require the buikwe-workspace preset. '
        'dashboard_id is optional for importing an ALREADY EXISTING Superset dashboard. '
        'The custom page renders live data without a Superset iframe. Call after '
        'the required data-source charts have been saved or reused, when the user asks to publish '
        'or share a custom UI dashboard. Returns the viewer link. Publishing updates '
        'your existing publication for this dashboard. Viewers still need a login and '
        'Superset permission to its source charts/datasets (and source dashboard when imported). Layout supports mouse, '
        'touch and keyboard drag-and-drop in the custom page. Use theme light or dark. '
        'All publications use the Vima Buikwe v1 design baseline. Optional design sets '
        'colors (accent, yes, no, unknown), a categorical palette, and groups '
        '(title, chart_ids) for related charts stacked into navigable tabbed cards. '
        'Use compact donut charts for Yes/No; preserve missing values separately. '
        'DEFAULT REQUIRED for new custom dashboards: configuration preset buikwe-workspace, '
        'source_chart_id (a raw current-client record chart), and workspace: selectors, '
        'site_field, country_field, record_columns, panels (slot, title, tabs field/label/chart_id), '
        'and workflow (field/value/label). This reuses the original gauge, five-column grid, '
        'compact side charts, map, sequential flow and record table. '
        'Workspace maps support optional facility_chart_id (raw facility coordinates, level and beds), '
        'boundary_chart_id (raw county/subcounty geometry_geojson), and region_label. '
        'Discover existing geographic datasets and create/reuse current-client raw reference charts; '
        'both references must be accessible and included in this publication. County outlines are dissolved from subcounty polygons. '
        'Facility colors and sizes reuse the established level-and-bed classification. '
        'Optionally supply layout to match a screenshot: ordered chart_id, span (4/6/12) '
        'and height in pixels for EVERY source chart. Omit layout to preserve manual edits. '
        'For a facility executive dashboard with map and shared PCN, hub, ownership and '
        'level filters, use configuration preset facility-network with source_chart_id '
        '(raw full survey: name_facility, hf_level, facility_type, ownership, lat, lon, '
        'maternal_childhealth, hiv_tb, labaratory, radiology, specialised, dental, optical, '
        'surgery, theatre, ambulance, mortuary) and network_chart_id (raw name_facility, '
        'pcn, hub). Both charts must be accessible publication sources. Use full source row limits. '
        'For generic population discovery use preset population-discovery with source_chart_id '
        '(raw person_id, facility_name, age, sex, blood_group, recorded screening and geography), '
        'boundary_chart_id (district, county, subcounty, geometry_geojson), facility_chart_id '
        '(facility_name, lat, lng), and default_facility. Superset calculates sequential funnel '
        'counts; the custom UI provides Overview, Discovery, Profile and authorized List/Detail. '
        'Donor willingness must not be described as contact consent or medical approval.'
    )
    input_schema = {
        'type': 'object',
        'properties': {
            'dashboard_id': {'type': 'integer', 'minimum': 1},
            'publication_id': {'type': 'string', 'format': 'uuid'},
            'title': {'type': 'string', 'minLength': 1, 'maxLength': 255},
            'theme': {'enum': ['light', 'dark']},
            'design': DESIGN_SCHEMA,
            'configuration': {'type': 'object', 'additionalProperties': False,
                'properties': {'preset': {'enum': ['facility-network', 'population-discovery', 'buikwe-workspace']},
                    'source_chart_id': {'type': 'integer', 'minimum': 1},
                    'workspace': WORKSPACE_SCHEMA,
                    'network_chart_id': {'type': 'integer', 'minimum': 1},
                    'boundary_chart_id': {'type': 'integer', 'minimum': 1},
                    'facility_chart_id': {'type': 'integer', 'minimum': 1},
                    'summary_chart_id': {'type': 'integer', 'minimum': 1},
                    'default_facility': {'type': 'string', 'maxLength': 250},
                    'native_dashboard_ids': {'type': 'array', 'minItems': 4, 'maxItems': 4,
                                             'items': {'type': 'integer', 'minimum': 1}},
                    'region_label': {'type': 'string', 'minLength': 1, 'maxLength': 100},
                    'map_bounds': {'type': 'array', 'minItems': 4, 'maxItems': 4,
                                   'items': {'type': 'number'}}},
                'required': ['preset', 'source_chart_id']},
            'layout': {'type': 'array', 'items': {
                'type': 'object', 'additionalProperties': False,
                'properties': {'chart_id': {'type': 'integer', 'minimum': 1},
                               'span': {'enum': [4, 6, 12]},
                               'height': {'type': 'integer', 'minimum': 200, 'maximum': 800}},
                'required': ['chart_id', 'span', 'height'],
            }},
        },
        'required': ['title', 'theme'],
        'additionalProperties': False,
    }

    def run(self, context: ToolContext, dashboard_id: int | None = None, title: str = '', theme: str = 'light', layout: list[dict[str, int]] | None = None, configuration: dict[str, Any] | None = None, design: dict[str, Any] | None = None, publication_id: str | None = None) -> dict[str, Any]:
        client = context.superset_client()
        from lifedata_ai.publication_sources import configuration_chart_ids
        existing = None
        if publication_id:
            from django.core.exceptions import ValidationError
            try:
                existing = DashboardPublication.objects.filter(
                    pk=publication_id, owner_username=context.identity.username).first()
            except (ValueError, ValidationError):
                raise ToolError('Invalid publication ID.', code='invalid_configuration')
            if existing is None:
                raise ToolError('No custom publication owned by you exists.', code='not_found')
            if dashboard_id is not None and dashboard_id != existing.superset_dashboard_id:
                raise ToolError('Cannot change the publication source dashboard.', code='invalid_configuration')
            dashboard_id = existing.superset_dashboard_id
        if dashboard_id is not None:
            dashboard = client.get_dashboard(dashboard_id)
            if not dashboard:
                raise ToolError('No accessible source dashboard exists.', code='not_found')
            charts = client.get_dashboard_charts(dashboard_id)
            missing = layout_chart_ids(dashboard) - {int(chart['id']) for chart in charts}
            if missing:
                raise ToolError('The Superset layout references charts missing from its chart membership.',
                                code='incomplete_dashboard_chart_membership',
                                details={'missing_chart_ids': sorted(missing)})
            if not existing:
                existing = DashboardPublication.objects.filter(
                    superset_dashboard_id=dashboard_id, owner_username=context.identity.username).first()
        else:
            effective_config = configuration if configuration is not None else (existing.configuration if existing else {})
            if effective_config.get('preset') != 'buikwe-workspace':
                raise ToolError('Custom-only publishing requires the reusable workspace configuration.',
                                code='workspace_configuration_required')
            ids = configuration_chart_ids(effective_config)
            if not ids:
                raise ToolError('Provide a current-client raw source chart.', code='empty_dashboard')
            charts = [client.get_chart(identifier) for identifier in ids]
            dashboard = {}
        if not charts:
            raise ToolError('Provide accessible source charts before publishing.', code='empty_dashboard')
        if not existing and configuration is None and layout is None:
            raise ToolError('New custom dashboards require the reusable buikwe-workspace template. '
                            'Inspect the current dataset, save a raw record chart, and provide workspace configuration. '
                            'Only provide an alternate layout when the user explicitly requests it.',
                            code='workspace_configuration_required')
        old_layout = {item['chart_id']: item for item in existing.layout} if existing else {}
        source_order = chart_order(dashboard, charts)
        if configuration is not None and 'design' in configuration:
            if design is None:
                design = configuration['design']
            configuration = {key: value for key, value in configuration.items() if key != 'design'}
            if not configuration:
                configuration = None
        saved_design = validate_design(design if design is not None else ((existing.configuration or {}).get('design', {}) if existing else {}), set(source_order))
        if configuration is not None:
            preset = configuration.get('preset')
            sources = ('source_chart_id',) if preset == 'buikwe-workspace' else ('source_chart_id', 'network_chart_id') if preset == 'facility-network' else (
                'source_chart_id', 'boundary_chart_id', 'facility_chart_id')
            optional = {'workspace', 'boundary_chart_id', 'facility_chart_id', 'region_label', 'map_bounds'} if preset == 'buikwe-workspace' else {'region_label', 'map_bounds'} if preset == 'facility-network' else {
                'region_label', 'map_bounds', 'default_facility', 'native_dashboard_ids', 'summary_chart_id'}
            if (preset not in {'facility-network', 'population-discovery', 'buikwe-workspace'}
                    or not {'preset', *sources}.issubset(configuration)
                    or set(configuration) - {'preset', *sources, *optional}
                    or any(type(configuration.get(key)) is not int or configuration[key] not in source_order
                           for key in sources)):
                label = 'Facility' if preset == 'facility-network' else 'Population'
                raise ToolError(f'{label} sources must be charts in this dashboard.', code='invalid_configuration')
            if preset == 'buikwe-workspace':
                for key in ('boundary_chart_id', 'facility_chart_id'):
                    if key in configuration and (type(configuration[key]) is not int or configuration[key] not in source_order):
                        raise ToolError('Geographic sources must be charts in this dashboard.', code='invalid_configuration')
                validate_workspace(configuration.get('workspace'), set(source_order))
            if 'native_dashboard_ids' in configuration:
                ids = configuration['native_dashboard_ids']
                if not isinstance(ids, list) or len(ids) != 4 or any(type(i) is not int or i < 1 for i in ids):
                    raise ToolError('Provide four native dashboard ids.', code='invalid_configuration')
                for native_id in ids:
                    client.get_dashboard(native_id)
            if 'map_bounds' in configuration:
                bounds = configuration['map_bounds']
                if (not isinstance(bounds, list) or len(bounds) != 4
                        or any(type(v) not in (int, float) for v in bounds)
                        or not (-90 <= bounds[0] < bounds[2] <= 90 and -180 <= bounds[1] < bounds[3] <= 180)):
                    raise ToolError('Map bounds must be south, west, north, east coordinates.', code='invalid_configuration')
        order = [item['chart_id'] for item in existing.layout if item['chart_id'] in source_order] if existing else []
        order += [chart_id for chart_id in source_order if chart_id not in order]
        default_layout = [old_layout.get(chart_id, {'chart_id': chart_id, 'span': 6, 'height': 320})
                  for chart_id in order]
        try:
            layout = validate_layout(layout if layout is not None else default_layout, set(source_order))
        except ValueError as exc:
            raise ToolError(str(exc), code='invalid_layout') from exc
        values = {'title': title, 'theme': theme, 'layout': layout,
                  'configuration': {**(configuration if configuration is not None else (existing.configuration if existing else {})), 'design': saved_design}}
        if dashboard_id is None:
            if existing:
                for key, value in values.items():
                    setattr(existing, key, value)
                existing.save(update_fields=[*values, 'updated_at'])
                publication = existing
            else:
                publication = DashboardPublication.objects.create(
                    superset_dashboard_id=None, owner_username=context.identity.username, **values)
        else:
            publication, _ = DashboardPublication.objects.update_or_create(
                superset_dashboard_id=dashboard_id, owner_username=context.identity.username, defaults=values)
        return {
            'type': 'published_dashboard', 'publication_id': str(publication.id),
            'title': publication.title, 'chart_count': len(layout),
            'url': f'/lifedata-ai/dashboards/{publication.id}/',
            'library_url': '/lifedata-ai/dashboards/',
            'message': 'Published as a custom dashboard with live Superset data.',
        }


class EditDashboardChartStyleTool(Tool):
    """Inspect targets or merge a single presentation override; never replace data/layout."""
    name = 'edit_dashboard_chart_style'
    mode = ToolMode.READ
    description = (
        'Inspect or edit ONE chart in an existing custom dashboard. Call with publication_id (preferred) or dashboard_id only '
        'to list the publication and exact editable targets. Then set target and style to apply '
        'a persistent isolated style patch. Target gauge changes only the workspace gauge; '
        'chart:<id> targets that chart; field:<name> targets a workspace variable without a source '
        'chart ID. Supports workspace, facility-network, population-discovery and ordinary publications. '
        'style supports color (gauge/bar/line/KPI), colors (yes/no/unknown/accent), palette. '
        'Use six-digit hex colors, e.g. green #16a34a. reset=true removes only that target override. '
        'Preserves every other chart, saved color, query, filter, tab and page layout. '
        'Inspect targets first; never guess an ID or change a global palette for a one-chart request.'
    )
    input_schema = {'type': 'object', 'additionalProperties': False, 'properties': {
        'dashboard_id': {'type': 'integer', 'minimum': 1},
        'publication_id': {'type': 'string', 'format': 'uuid'}, 'target': {'type': 'string'},
        'style': DESIGN_SCHEMA['properties']['chart_styles']['additionalProperties'],
        'reset': {'type': 'boolean'}}, 'anyOf': [{'required': ['dashboard_id']}, {'required': ['publication_id']}]}

    def run(self, context, dashboard_id=None, target=None, style=None, reset=False, publication_id=None):
        import copy
        from django.db import transaction
        client = context.superset_client()
        from lifedata_ai.publication_sources import publication_charts
        from django.core.exceptions import ValidationError
        with transaction.atomic():
            lookup = {'pk': publication_id} if publication_id else {'superset_dashboard_id': dashboard_id}
            try:
                publication = DashboardPublication.objects.select_for_update().filter(
                    owner_username=context.identity.username, **lookup).first()
            except (ValueError, ValidationError):
                raise ToolError('Invalid publication ID.', code='invalid_configuration')
            if publication is None:
                raise ToolError('No custom dashboard owned by you exists for this source.', code='not_found')
            if dashboard_id is not None and dashboard_id != publication.superset_dashboard_id:
                raise ToolError('Publication and dashboard IDs do not match.', code='invalid_configuration')
            charts = publication_charts(client, publication)
            chart_ids = {int(c['id']) for c in charts}
            config = copy.deepcopy(publication.configuration or {})
            preset = config.get('preset')
            targets = []
            if preset == 'buikwe-workspace':
                targets = [{'target': 'gauge', 'title': 'Headline gauge'}]
                for panel in config['workspace']['panels']:
                    for tab in panel['tabs']:
                        if tab.get('chart_id') and tab['chart_id'] not in chart_ids:
                            continue
                        identifier = 'chart:' + str(tab['chart_id']) if tab.get('chart_id') else 'field:' + tab['field'] + (':' + tab['measure'] + ':' + tab.get('aggregation', 'sum') if tab.get('measure') else '')
                        targets.append({'target': identifier, 'title': panel['title'] + ' / ' + tab['label']})
            elif preset == 'facility-network':
                services = [('maternal_childhealth', 'Maternal & child health'), ('hiv_tb', 'HIV & TB'),
                    ('labaratory', 'Laboratory'), ('ambulance', 'Ambulance'), ('dental', 'Dental'),
                    ('surgery', 'Surgery'), ('radiology', 'Radiology'), ('specialised', 'Specialist services'),
                    ('optical', 'Optical'), ('theatre', 'Theatre'), ('mortuary', 'Mortuary'),
                    ('facility_type', 'Facility category'), ('hf_level', 'Facility level'), ('ownership', 'Ownership')]
                targets = [{'target': 'field:' + field, 'title': title} for field, title in services]
                targets += [{'target': 'field:kpi-' + str(i), 'title': title} for i, title in enumerate(
                    ['Survey records', 'Distinct facility names', 'Primary care networks', 'Linked hubs'])]
            elif preset == 'population-discovery':
                from pathlib import Path
                panels = json.loads((Path(__file__).resolve().parent.parent / 'population_panels.json').read_text())
                targets = [{'target': 'gauge', 'title': 'Number of People gauge'}]
                targets += [{'target': 'field:' + field, 'title': panel['title'] + ' / ' + label}
                            for panel in panels for field, label in panel['tabs']]
                targets += [{'target': 'field:' + f, 'title': f.replace('_', ' ').title()} for f in ['age', 'sex', 'district', 'distance_km']]
            elif not preset:
                targets = [{'target': 'chart:' + str(c['id']), 'title': c.get('slice_name', str(c['id']))}
                           for c in charts]
            else:
                raise ToolError('This publication template has no editable targets.', code='unsupported_template')
            if target is not None:
                if target not in {item['target'] for item in targets}:
                    raise ToolError('Choose an editable target returned by this tool.', code='invalid_chart_target')
                if reset and style is not None:
                    raise ToolError('Use either reset or a style patch.', code='invalid_design')
                if not reset and (not isinstance(style, dict) or not style):
                    raise ToolError('Provide a nonempty style patch.', code='invalid_design')
                design = config.setdefault('design', {'baseline': 'vima-buikwe-v1'})
                overrides = design.setdefault('chart_styles', {})
                if reset:
                    overrides.pop(target, None)
                else:
                    merged = {**overrides.get(target, {}), **style}
                    if 'colors' in style:
                        merged['colors'] = {**overrides.get(target, {}).get('colors', {}), **style['colors']}
                    overrides[target] = merged
                config['design'] = validate_design(design, chart_ids)
                publication.configuration = config
                publication.save(update_fields=['configuration'])
            elif style is not None or reset:
                raise ToolError('Select a chart target first.', code='invalid_chart_target')
            return {'type': 'published_dashboard' if target else 'dashboard_style_targets',
                    'publication_id': str(publication.id), 'title': publication.title,
                    'chart_count': len(chart_ids), 'source_chart_ids': sorted(chart_ids),
                    'source_chart_id': config.get('source_chart_id'),
                    'url': f'/lifedata-ai/dashboards/{publication.id}/',
                    'targets': targets, 'design': config.get('design', {}),
                    'updated_target': target, 'message': 'Updated only the selected chart style.' if target else 'Choose an exact chart target.'}
