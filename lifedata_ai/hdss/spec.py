"""The per-dashboard spec of the hdss-site template, and its validation against bindings.yaml.

A spec is a few kB of data: title, site, the registered dataset, its role/value-map bindings (copied
from bindings.yaml and required to stay identical to it), which slots and filters to show, deep-dive
addresses and the privacy block. It holds no layout and no HTML; the template owns those.

validate_spec() runs before every save and refuses with a precise reason when:
  the dataset is not registered / not readable, a bound column does not exist in Superset, a binding
  differs from the registry, a chart dimension is not a dimension-role column, a key/personal/free-text
  column is used anywhere, a KPI does not resolve to exactly one category, or the site value is absent.
"""
from __future__ import annotations

import copy
import re
from typing import Any
from urllib.parse import urlsplit

import jsonschema

from lifedata_ai.hdss.registry import (DIMENSIONS, GEOGRAPHY_LEVELS, PRIVATE_ROLES, ROLES, DatasetBinding,
                                       Unbound, binding_for_dataset, load_bindings)

TEMPLATE = 'hdss-site'
PRESET = 'hdss-site'
SLOTS = ('header.count', 'kpi.births', 'kpi.deaths', 'kpi.in_migration', 'kpi.out_migration', 'kpi.under_five',
         'card.persons_households', 'card.age_gender', 'card.coordinates', 'card.cooking_fuel', 'card.water_source',
         'card.education', 'card.occupation', 'card.roof_floor', 'card.bednet_toilet', 'card.migration_water',
         'card.marital', 'card.lifeconnect', 'card.population_households', 'card.assets', 'footer.counts')
FILTERS = (*GEOGRAPHY_LEVELS, 'time_period', 'time_range', *DIMENSIONS)
DEEP_DIVE_CARDS = ('Births', 'Deaths', 'In-migration', 'Out-migration', 'Under five')
#: KPI slot -> (dimension, the one category it counts).
KPI_SLOTS = {'kpi.births': ('event', 'Births'), 'kpi.deaths': ('event', 'Deaths'),
             'kpi.in_migration': ('event', 'In-migration'), 'kpi.out_migration': ('event', 'Out-migration'),
             'kpi.under_five': ('age_band', '0-4')}
#: Card slot -> dimensions it can show (bound if ANY is bound; a switch card hides unbound variables).
CARD_DIMENSIONS = {'card.age_gender': ('age_band', 'sex'), 'card.cooking_fuel': ('fuel',), 'card.water_source': ('water',),
                   'card.education': ('education',), 'card.occupation': ('occupation',), 'card.roof_floor': ('roof', 'floor'),
                   'card.bednet_toilet': ('bednet', 'toilet'), 'card.migration_water': ('migration', 'water'),
                   'card.marital': ('marital',), 'card.assets': ('asset',)}
#: Column names that are never allowed in a spec, whatever role a binding claims for them.
PERSONAL_NAME = re.compile(
    r'(^|_)(first_?name|last_?name|surname|full_?name|name|names|.*_nam|phone_?(number|no|num)|telephone|tel|msisdn|'
    r'mobile_?(number|no|num)|contact|'
    r'email|e_mail|address|national_id|nin|passport|comment|comments|note|notes|remark|remarks|free_?text|'
    r'specify|other_specify|description)($|_)', re.IGNORECASE)
MAX_TITLE = 120

_TEXT = {'type': 'string', 'maxLength': 200}
SPEC_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['template', 'version', 'title', 'status', 'dataset', 'site', 'entities', 'geography', 'dimensions',
                 'slots', 'filters', 'deep_dive', 'privacy'],
    'properties': {
        'template': {'const': TEMPLATE}, 'version': {'const': 1},
        'title': {'type': 'string', 'minLength': 1, 'maxLength': MAX_TITLE},
        'status': {'enum': ['draft', 'published']},
        'dataset': {'type': 'object', 'additionalProperties': False, 'required': ['key', 'superset_dataset_id'],
                    'properties': {'key': _TEXT, 'superset_dataset_id': {'type': 'integer', 'minimum': 1}}},
        'site': {'type': 'object', 'additionalProperties': False, 'required': ['column', 'value'],
                 'properties': {'column': {'type': ['string', 'null'], 'maxLength': 128}, 'value': _TEXT}},
        'entities': {'type': 'object', 'additionalProperties': False, 'required': ['persons'], 'properties': {
            'persons': {'type': 'object', 'additionalProperties': False, 'required': ['grain', 'roles'], 'properties': {
                'grain': {'const': 'person'},
                'roles': {'type': 'object', 'additionalProperties': False,
                          'properties': {r: {'type': ['string', 'null'], 'maxLength': 128} for r in ROLES}}}}}},
        'geography': {'type': 'object', 'additionalProperties': False,
                      'properties': {g: {'type': ['string', 'null'], 'maxLength': 128} for g in GEOGRAPHY_LEVELS}},
        'dimensions': {'type': 'object', 'additionalProperties': False,
                       'properties': {d: {'type': ['object', 'null']} for d in DIMENSIONS}},
        'slots': {'type': 'array', 'uniqueItems': True, 'items': {'enum': list(SLOTS)}},
        'filters': {'type': 'array', 'uniqueItems': True, 'items': {'enum': list(FILTERS)}},
        'deep_dive': {'type': 'object', 'additionalProperties': False,
                      'properties': {c: {'type': ['string', 'null'], 'maxLength': 500} for c in DEEP_DIVE_CARDS}},
        # Optional dashed reference line on the Population and Household bars (the approved demo uses 530).
        'population_reference': {'type': ['integer', 'null'], 'minimum': 1},
        'privacy': {'type': 'object', 'additionalProperties': False,
                    'required': ['person_level_rows_to_browser', 'min_cell_size', 'synthetic'],
                    'properties': {'person_level_rows_to_browser': {'const': False},
                                   'min_cell_size': {'type': 'integer', 'minimum': 0, 'maximum': 1000},
                                   'synthetic': {'type': 'boolean'}}},
    },
}


class SpecError(ValueError):
    """The spec cannot be saved; the message says exactly why."""


def build_spec(binding: DatasetBinding, title: str, deep_dive: dict[str, str | None] | None = None,
               slots: list[str] | None = None, filters: list[str] | None = None,
               population_reference: int | None = None) -> dict[str, Any]:
    """Fill a spec from the registry. Unbound roles/levels/dimensions are null, never improvised."""
    dimensions = {name: (dim.as_dict() if (dim := binding.dimension(name)) else None) for name in DIMENSIONS}
    return {
        'template': TEMPLATE, 'version': 1, 'title': title, 'status': 'draft',
        'dataset': {'key': binding.key, 'superset_dataset_id': binding.superset_dataset_id},
        'site': {'column': binding.site_column, 'value': binding.site_value},
        'entities': {'persons': {'grain': 'person', 'roles': {r: binding.role(r) for r in ROLES}}},
        'geography': {g: binding.level(g) for g in GEOGRAPHY_LEVELS},
        'dimensions': dimensions,
        'slots': list(slots) if slots is not None else list(SLOTS),
        'filters': list(filters) if filters is not None else list(FILTERS),
        'deep_dive': {card: (deep_dive or {}).get(card) for card in DEEP_DIVE_CARDS},
        'population_reference': population_reference,
        'privacy': {'person_level_rows_to_browser': False, 'min_cell_size': binding.min_cell_size,
                    'synthetic': binding.synthetic},
    }


def _deep_dive_ok(address: str) -> bool:
    if address.startswith('/') and not address.startswith('//'):
        return True
    parts = urlsplit(address)
    return parts.scheme in ('http', 'https') and bool(parts.netloc)


def spec_columns(spec: dict[str, Any]) -> set[str]:
    """Every column a spec touches."""
    columns = {c for c in spec['entities']['persons']['roles'].values() if c}
    columns |= {c for c in spec['geography'].values() if c}
    if spec['site']['column']:
        columns.add(spec['site']['column'])
    for dim in spec['dimensions'].values():
        if not dim:
            continue
        if dim.get('column'):
            columns.add(dim['column'])
        else:
            for category in dim.get('categories', {}).values():
                columns |= {c for c in category if c != 'unbound'}
    return columns


def coverage(binding: DatasetBinding, spec: dict[str, Any]) -> dict[str, Any]:
    """slot/filter -> bound or the registered reason it cannot bind."""
    def reason(value) -> dict[str, str]:
        return value.as_dict() if isinstance(value, Unbound) else {'status': 'no_source', 'reason': 'Not registered.'}

    slots: dict[str, Any] = {}
    for slot in spec['slots']:
        if slot in KPI_SLOTS:
            name, label = KPI_SLOTS[slot]
            dim = binding.dimension(name)
            if dim and dim.category(label):
                slots[slot] = 'bound'
            elif dim:
                missing = dict(dim.unbound_categories).get(label)
                slots[slot] = missing.as_dict() if missing else {'status': 'no_source', 'reason': f'No {label} category.'}
            else:
                slots[slot] = reason(binding.dimensions.get(name))
        elif slot in CARD_DIMENSIONS:
            names = CARD_DIMENSIONS[slot]
            bound = [n for n in names if binding.dimension(n)]
            needs_all = slot == 'card.age_gender'
            if (needs_all and len(bound) == len(names)) or (not needs_all and bound):
                part = {n: reason(binding.dimensions.get(n)) for n in names if n not in bound}
                part.update({f'{n}: {label}': why.as_dict() for n in bound
                             for label, why in binding.dimension(n).unbound_categories})
                slots[slot] = 'bound' if not part else {'status': 'partial', 'hidden': part}
            else:
                slots[slot] = {'status': 'unbound', 'parts': {n: reason(binding.dimensions.get(n)) for n in names
                                                              if n not in bound}}
        elif slot == 'card.coordinates':
            missing = {r: reason(binding.roles.get(r)) for r in ('latitude', 'longitude') if not binding.role(r)}
            if not binding.level('village'):
                missing['village'] = reason(binding.geography.get('village'))
            slots[slot] = 'bound' if not missing else {'status': 'unbound', 'parts': missing}
        else:  # header, footer, gauge, population bars, LifeConnect: rows + households only
            slots[slot] = 'bound'
    filters: dict[str, Any] = {}
    for name in spec['filters']:
        if name in GEOGRAPHY_LEVELS:
            filters[name] = 'bound' if binding.level(name) else reason(binding.geography.get(name))
        elif name in ('time_period', 'time_range'):
            filters[name] = 'bound' if binding.role('record_date') else reason(binding.roles.get('record_date'))
        else:
            filters[name] = 'bound' if binding.dimension(name) else reason(binding.dimensions.get(name))
    return {'slots': slots, 'filters': filters}


def validate_spec(spec: Any, client=None) -> tuple[DatasetBinding, dict[str, Any]]:
    """Return (binding, coverage) or raise SpecError naming the first problem. `client` checks Superset."""
    try:
        jsonschema.validate(spec, SPEC_SCHEMA)
    except jsonschema.ValidationError as exc:
        where = '.'.join(str(p) for p in exc.absolute_path) or 'spec'
        raise SpecError(f'{where}: {exc.message}') from exc
    dataset_id = spec['dataset']['superset_dataset_id']
    binding = binding_for_dataset(dataset_id)
    if binding is None:
        registered = ', '.join(f'{b.superset_dataset_id} ({b.label})' for b in load_bindings().values())
        raise SpecError(f'Dataset {dataset_id} is not registered for the HDSS template. Registered: {registered}.')
    if spec['dataset']['key'] != binding.key:
        raise SpecError(f"Dataset {dataset_id} is registered as {binding.key!r}, not {spec['dataset']['key']!r}.")
    if not spec['title'].strip():
        raise SpecError('The title is empty.')

    for column in sorted(spec_columns(spec)):
        if PERSONAL_NAME.search(column):
            raise SpecError(f'Column {column!r} looks like a personal or free-text field and cannot be used.')
        role = binding.columns.get(column)
        if role is None:
            raise SpecError(f'Column {column!r} is not registered for dataset {dataset_id}.')
        if role in PRIVATE_ROLES:
            raise SpecError(f'Column {column!r} has role {role}; identifiers and personal fields cannot be used.')

    expected = build_spec(binding, spec['title'])
    if spec['site'] != expected['site']:
        raise SpecError(f"Site must be {expected['site']} for this dataset (the registered site binding).")
    for role, column in spec['entities']['persons']['roles'].items():
        if column != binding.role(role):
            raise SpecError(f'Role {role} is bound to {binding.role(role)!r} in the registry, not {column!r}.')
    for level, column in spec['geography'].items():
        if column != binding.level(level):
            raise SpecError(f'Geography {level} is bound to {binding.level(level)!r} in the registry, not {column!r}.')
    for name, value in spec['dimensions'].items():
        dim = binding.dimension(name)
        if value is None:
            continue
        if dim is None:
            unbound = binding.dimensions.get(name)
            why = unbound.reason if isinstance(unbound, Unbound) else 'not registered'
            raise SpecError(f'Dimension {name} has no registered source ({why}); it cannot be bound in a spec.')
        if value != dim.as_dict():
            raise SpecError(f'Dimension {name} must use the registered value map exactly; ask the data owner to change '
                            f'bindings.yaml instead of editing it in a spec.')
        roles = {binding.columns.get(c) for c in dim.columns}
        if roles != {'dimension'}:
            raise SpecError(f'Dimension {name} uses a column that is not a dimension-role column.')
    for slot, (name, label) in KPI_SLOTS.items():
        dim = binding.dimension(name)
        if slot in spec['slots'] and dim and spec['dimensions'].get(name) is not None:
            if [c.label for c in dim.categories].count(label) != 1:
                raise SpecError(f'{slot} must resolve to exactly one {name} category ({label!r}).')
    for card, address in spec['deep_dive'].items():
        if address is not None and (not address.strip() or not _deep_dive_ok(address.strip())):
            raise SpecError(f'Deep dive for {card} must be an https:// address or a /path on this site.')
    privacy = spec['privacy']
    if privacy['synthetic'] != binding.synthetic:
        raise SpecError(f'privacy.synthetic must be {binding.synthetic} (registered).')
    if privacy['min_cell_size'] < binding.min_cell_size:
        raise SpecError(f'privacy.min_cell_size cannot be below the registered {binding.min_cell_size}.')

    if client is not None:
        dataset = client.get_dataset(dataset_id)
        if not dataset:
            raise SpecError(f'Dataset {dataset_id} does not exist or you cannot read it.')
        available = {c.get('column_name') for c in dataset.get('columns', [])}
        missing = sorted(spec_columns(spec) - available)
        if missing:
            raise SpecError(f'Column(s) {missing} do not exist in Superset dataset {dataset_id}.')
        if binding.site_column:
            from lifedata_ai.hdss.engine import COUNT, superset_executor
            rows = superset_executor(client)(dataset_id, [{
                'columns': [], 'metrics': [COUNT], 'time_range': 'No filter', 'row_limit': 1, 'orderby': [],
                'filters': [{'col': binding.site_column, 'op': '==', 'val': binding.site_value}]}])[0]
            if not rows or not int(rows[0].get('n') or 0):
                raise SpecError(f'Site {binding.site_value!r} has no records in dataset {dataset_id}.')
    return binding, coverage(binding, spec)


def effective(spec: dict[str, Any], binding: DatasetBinding) -> dict[str, Any]:
    """The spec as the page renders it: slots/filters that cannot bind are dropped (and listed)."""
    report = coverage(binding, spec)
    out = copy.deepcopy(spec)
    out['slots'] = [s for s, v in report['slots'].items() if v == 'bound' or (isinstance(v, dict) and v.get('status') == 'partial')]
    out['filters'] = [f for f, v in report['filters'].items() if v == 'bound']
    out['coverage'] = report
    return out
