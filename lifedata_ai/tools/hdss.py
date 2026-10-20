"""Vima's tools for the hdss-site template. Vima fills a spec from the registry; it never writes layout."""
from __future__ import annotations

from typing import Any

from lifedata_ai.hdss.registry import BindingsError, binding_for_dataset, load_bindings
from lifedata_ai.hdss.spec import DEEP_DIVE_CARDS, FILTERS, SLOTS, SpecError, build_spec
from lifedata_ai.hdss import service
from lifedata_ai.superset_client import SupersetError
from lifedata_ai.tools.base import Tool, ToolContext, ToolError, ToolMode


class CreateHdssDashboardTool(Tool):
    name = 'create_hdss_dashboard'
    mode = ToolMode.READ  # saves a LifeData draft page; no Superset write
    description = (
        'Create (or update) an HDSS site dashboard from the approved HDSS template. Use this for ANY request for an '
        'HDSS dashboard (Dodowa, Navrongo, Manhica, SINCEP, ...). Pass the registered dataset (its key or Superset '
        'dataset id), the site, and a title; optionally deep-dive addresses for the five KPI cards. The template '
        'defines the whole layout and every interaction: never author layout, charts, tabs or dataset SQL for it. '
        'The spec is filled from the HDSS bindings registry and validated before saving; an unregistered dataset, a '
        'missing column or a personal column is refused with the reason. It is saved as a DRAFT and read back: report '
        '"verified" or what failed, and list every unbound slot with its reason instead of improvising. Ask the user '
        'before publishing (publish_hdss_dashboard). Ask at most one question.')
    input_schema = {
        'type': 'object', 'additionalProperties': False,
        'required': ['dataset', 'site', 'title', 'spec'],
        'properties': {
            'dataset': {'type': 'string', 'description': 'Registered dataset key (e.g. navrongo-hdss) or Superset dataset id as text.'},
            'site': {'type': 'string', 'description': 'The site name the user asked for, e.g. "Navrongo".'},
            'title': {'type': 'string', 'description': 'Page title. Treated as plain text.'},
            'spec': {'type': 'object', 'additionalProperties': False,
                     'required': ['deep_dive', 'publication_id'],
                     'description': 'Only what the template lets a dashboard choose. Use {} values when not needed.',
                     'properties': {
                         'deep_dive': {'type': 'object', 'additionalProperties': False, 'required': [],
                                       'properties': {c: {'type': 'string'} for c in DEEP_DIVE_CARDS}},
                         'publication_id': {'type': 'string', 'description': 'Existing HDSS draft to update, or "".'}}},
        },
    }

    def run(self, context: ToolContext, dataset: str, site: str, title: str, spec: dict[str, Any]) -> dict[str, Any]:
        bindings = load_bindings()
        binding = bindings.get(dataset) or (binding_for_dataset(int(dataset)) if dataset.strip().isdigit() else None)
        if binding is None:
            registered = [{'key': b.key, 'dataset_id': b.superset_dataset_id, 'site': b.site_value} for b in bindings.values()]
            raise ToolError(f'{dataset!r} is not registered for the HDSS template. Registered datasets: {registered}. '
                            'A data owner must add it to lifedata_ai/hdss/bindings.yaml first.', code='not_registered')
        wanted = site.strip().lower()
        if wanted and wanted not in binding.site_value.lower() and wanted not in binding.key:
            raise ToolError(f'Dataset {binding.key} holds site {binding.site_value!r}, not {site!r}.', code='site_mismatch')
        deep_dive = {k: v for k, v in (spec.get('deep_dive') or {}).items() if v}
        full = build_spec(binding, title.strip()[:120], deep_dive)
        try:
            result = service.save(context.superset_client(), context.username, full,
                                  spec.get('publication_id') or None)
        except SpecError as exc:
            raise ToolError(f'Refused: {exc}', code='invalid_spec') from exc
        except SupersetError as exc:
            raise ToolError(f'Superset refused: {exc}', code='superset_error') from exc
        result['message'] = ('Saved as a DRAFT and read back: ' + ('verified.' if result['verified'] else 'NOT verified.')
                             + ' Tell the user which slots are unbound (listed) and ask before publishing.')
        return result


class CreateMasterDashboardTool(Tool):
    name = 'create_master_dashboard'
    mode = ToolMode.READ  # saves a LifeData draft page; no Superset write
    description = (
        'Create (or update) a Master dashboard from the approved three-tab Master template (Overview, LifeHealth '
        'Sentinel, Geospatial Cluster) with one shared filter state. Pass a registered Master source key (its datasets '
        'are the registered HDSS site data, or the synthetic demo) and a title. The template defines the tabs, layout and '
        'every interaction: never author layout, charts or tabs for it. The spec is filled from the Master registry and '
        'validated before saving; tabs whose slots have no registered source are hidden, and the reasons are returned. '
        'Saved as a DRAFT and read back: say "verified" only if the result says so, list the unbound slots, and ask before '
        'publishing (publish_hdss_dashboard). Ask at most one question.')
    input_schema = {
        'type': 'object', 'additionalProperties': False, 'required': ['datasets', 'title', 'spec'],
        'properties': {
            'datasets': {'type': 'string', 'description': 'Registered Master source key, e.g. master-hdss-sites or master-demo.'},
            'title': {'type': 'string', 'description': 'Page title. Treated as plain text.'},
            'spec': {'type': 'object', 'additionalProperties': False, 'required': ['publication_id'],
                     'properties': {'publication_id': {'type': 'string', 'description': 'Existing Master draft to update, or "".'}}},
        },
    }

    def run(self, context: ToolContext, datasets: str, title: str, spec: dict[str, Any]) -> dict[str, Any]:
        from lifedata_ai.hdss import master
        try:
            full = master.build_master_spec(datasets.strip(), title.strip()[:120])
            result = service.save(context.superset_client(), context.username, full, spec.get('publication_id') or None)
        except SpecError as exc:
            raise ToolError(f'Refused: {exc}', code='invalid_spec') from exc
        except SupersetError as exc:
            raise ToolError(f'Superset refused: {exc}', code='superset_error') from exc
        result['message'] = ('Saved as a DRAFT and read back: ' + ('verified.' if result['verified'] else 'NOT verified.')
                             + ' Tell the user which tabs/slots are unbound (listed) and ask before publishing.')
        return result


class PublishHdssDashboardTool(Tool):
    name = 'publish_hdss_dashboard'
    mode = ToolMode.READ
    description = ('Publish an HDSS template draft (or return it to draft). Only call after the user has explicitly '
                   'agreed to publish in this conversation. Reads the saved state back and reports it.')
    input_schema = {'type': 'object', 'additionalProperties': False, 'required': ['publication_id', 'status'],
                    'properties': {'publication_id': {'type': 'string'}, 'status': {'enum': ['published', 'draft']}}}

    def run(self, context: ToolContext, publication_id: str, status: str) -> dict[str, Any]:
        try:
            return service.set_status(context.username, publication_id, status)
        except (SpecError, BindingsError, ValueError) as exc:
            raise ToolError(str(exc), code='invalid_request') from exc


__all__ = ['CreateHdssDashboardTool', 'PublishHdssDashboardTool', 'SLOTS', 'FILTERS']
