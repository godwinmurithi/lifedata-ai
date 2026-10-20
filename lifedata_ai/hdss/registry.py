"""Load and check the HDSS template's scoped bindings (bindings.yaml).

Only the hdss-site template reads this. A load error is a hard failure: a half-understood binding
would show wrong numbers, which is worse than showing none.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

BINDINGS_PATH = Path(__file__).resolve().parent / 'bindings.yaml'

COLUMN_ROLES = {'key', 'household_key', 'site', 'geography', 'coordinate', 'temporal', 'dimension', 'personal', 'internal'}
#: Roles whose columns never appear in a query's GROUP BY or filters, and never reach a response.
PRIVATE_ROLES = {'key', 'personal', 'internal'}
GEOGRAPHY_LEVELS = ('district', 'county', 'subcounty', 'parish', 'village')
#: Template dimension keys, in the order the filter state and chips use them.
DIMENSIONS = ('age_band', 'sex', 'event', 'fuel', 'water', 'education', 'occupation', 'marital',
              'migration', 'bednet', 'roof', 'floor', 'toilet', 'asset', 'update_cycle')
#: role -> required column role.
ROLES = {'household_id': 'household_key', 'record_date': 'temporal', 'latitude': 'coordinate', 'longitude': 'coordinate'}
SENSITIVITY = {'aggregate_only'}


class BindingsError(ValueError):
    """bindings.yaml is malformed or inconsistent."""


@dataclass(frozen=True)
class Unbound:
    """A role, level, dimension or category with no usable source. `kind` is shown to the user."""
    kind: str  # 'needs_definition' | 'no_source'
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {'status': self.kind, 'reason': self.reason}


@dataclass(frozen=True)
class Condition:
    """One column test: value IN values, or min <= value < max (numeric)."""
    column: str
    values: tuple[str, ...] | None = None
    min: float | None = None
    max: float | None = None

    def superset_filters(self) -> list[dict[str, Any]]:
        if self.values is not None:
            return [{'col': self.column, 'op': 'IN', 'val': list(self.values)}]
        out = []
        if self.min is not None:
            out.append({'col': self.column, 'op': '>=', 'val': self.min})
        if self.max is not None:
            out.append({'col': self.column, 'op': '<', 'val': self.max})
        return out

    def matches(self, value: Any) -> bool:
        if value is None:
            return False
        if self.values is not None:
            return _text(value) in self.values
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        return (self.min is None or number >= self.min) and (self.max is None or number < self.max)

    def as_dict(self) -> Any:
        if self.values is not None:
            return list(self.values)
        return {k: v for k, v in (('min', self.min), ('max', self.max)) if v is not None}


@dataclass(frozen=True)
class Category:
    label: str
    conditions: tuple[Condition, ...]  # all must hold

    def matches(self, row: dict[str, Any]) -> bool:
        return all(c.matches(row.get(c.column)) for c in self.conditions)

    def superset_filters(self) -> list[dict[str, Any]]:
        return [f for c in self.conditions for f in c.superset_filters()]


@dataclass(frozen=True)
class Dimension:
    key: str
    categories: tuple[Category, ...]
    multi: bool = False
    single_column: str | None = None
    unbound_categories: tuple[tuple[str, Unbound], ...] = ()

    @property
    def columns(self) -> list[str]:
        return list(dict.fromkeys(c.column for cat in self.categories for c in cat.conditions))

    @property
    def labels(self) -> list[str]:
        return [c.label for c in self.categories]

    def category(self, label: str) -> Category | None:
        return next((c for c in self.categories if c.label == label), None)

    def assign(self, row: dict[str, Any]) -> list[str]:
        """Labels a grouped result row belongs to (at most one unless multi)."""
        hits = [c.label for c in self.categories if c.matches(row)]
        return hits if self.multi else hits[:1]

    def as_dict(self) -> dict[str, Any]:
        """The canonical value map, as stored in a spec and compared on validation."""
        out: dict[str, Any] = {'multi': self.multi} if self.multi else {}
        if self.single_column:
            out['column'] = self.single_column
            out['categories'] = {c.label: c.conditions[0].as_dict() for c in self.categories}
        else:
            out['categories'] = {c.label: {cond.column: cond.as_dict() for cond in c.conditions} for c in self.categories}
        for label, unbound in self.unbound_categories:
            out['categories'][label] = {'unbound': unbound.reason}
        return out


@dataclass
class DatasetBinding:
    key: str
    superset_dataset_id: int
    label: str
    grain: str
    synthetic: bool
    sensitivity: str
    min_cell_size: int
    site_column: str | None
    site_value: str
    columns: dict[str, str]
    roles: dict[str, str | Unbound]
    geography: dict[str, str | Unbound]
    dimensions: dict[str, Dimension | Unbound]
    reference_date: str | None = None
    notes: list[str] = field(default_factory=list)

    def role(self, name: str) -> str | None:
        value = self.roles.get(name)
        return value if isinstance(value, str) else None

    def level(self, name: str) -> str | None:
        value = self.geography.get(name)
        return value if isinstance(value, str) else None

    def dimension(self, name: str) -> Dimension | None:
        value = self.dimensions.get(name)
        return value if isinstance(value, Dimension) else None

    @property
    def bound_levels(self) -> list[str]:
        return [level for level in GEOGRAPHY_LEVELS if self.level(level)]

    def coverage(self) -> dict[str, Any]:
        """role/level/dimension -> 'bound' or {status, reason}; the source of every coverage table."""
        def state(value):
            return 'bound' if not isinstance(value, Unbound) else value.as_dict()
        out = {'roles': {r: state(self.roles.get(r, Unbound('no_source', 'Not registered.'))) for r in ROLES},
               'geography': {g: state(self.geography.get(g, Unbound('no_source', 'Not registered.'))) for g in GEOGRAPHY_LEVELS},
               'dimensions': {}}
        for name in DIMENSIONS:
            value = self.dimensions.get(name, Unbound('no_source', 'Not registered.'))
            out['dimensions'][name] = state(value)
            if isinstance(value, Dimension) and value.unbound_categories:
                out['dimensions'][name] = {'status': 'bound', 'unbound_categories': {k: u.as_dict() for k, u in value.unbound_categories}}
        return out


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return 'Yes' if value else 'No'
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _unbound(raw: Any, where: str) -> Unbound | None:
    if not isinstance(raw, dict):
        return None
    kinds = [k for k in ('needs_definition', 'no_source') if k in raw]
    if not kinds:
        return None
    if len(raw) != 1 or not isinstance(raw[kinds[0]], str) or not raw[kinds[0]].strip():
        raise BindingsError(f'{where}: an unbound entry is exactly one of needs_definition/no_source with a reason.')
    return Unbound(kinds[0], raw[kinds[0]].strip())


def _condition(column: str, raw: Any, where: str) -> Condition:
    if isinstance(raw, list):
        if not raw or not all(isinstance(v, (str, int, float)) and not isinstance(v, bool) for v in raw):
            raise BindingsError(f'{where}: values must be a non-empty list of strings or numbers.')
        return Condition(column, values=tuple(_text(v) for v in raw))
    if isinstance(raw, dict) and raw and set(raw) <= {'min', 'max'}:
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in raw.values()):
            raise BindingsError(f'{where}: min/max must be numbers.')
        if 'min' in raw and 'max' in raw and raw['min'] >= raw['max']:
            raise BindingsError(f'{where}: min must be below max.')
        return Condition(column, min=raw.get('min'), max=raw.get('max'))
    raise BindingsError(f'{where}: expected a list of values or {{min, max}}.')


def _dimension(name: str, raw: Any, columns: dict[str, str], where: str) -> Dimension | Unbound:
    unbound = _unbound(raw, where)
    if unbound:
        return unbound
    if not isinstance(raw, dict) or not isinstance(raw.get('categories'), dict) or not raw['categories']:
        raise BindingsError(f'{where}: a bound dimension needs a non-empty categories mapping.')
    extra = set(raw) - {'column', 'categories', 'multi'}
    if extra:
        raise BindingsError(f'{where}: unknown keys {sorted(extra)}.')
    column = raw.get('column')
    categories, unbound_categories = [], []
    for label, spec in raw['categories'].items():
        label = _text(label)
        cwhere = f'{where}.{label}'
        if isinstance(spec, dict) and set(spec) == {'unbound'}:
            unbound_categories.append((label, Unbound('no_source', str(spec['unbound']))))
            continue
        if column:
            conditions = (_condition(column, spec, cwhere),)
        else:
            if not isinstance(spec, dict) or not spec:
                raise BindingsError(f'{cwhere}: without a dimension column each category maps column -> values.')
            conditions = tuple(_condition(str(col), value, f'{cwhere}.{col}') for col, value in spec.items())
        for condition in conditions:
            if columns.get(condition.column) != 'dimension':
                raise BindingsError(f'{cwhere}: column {condition.column!r} is not declared with role dimension.')
        categories.append(Category(label, conditions))
    if not categories:
        raise BindingsError(f'{where}: every category is unbound; mark the dimension itself unbound.')
    labels = [c.label for c in categories] + [u[0] for u in unbound_categories]
    if len(labels) != len(set(labels)):
        raise BindingsError(f'{where}: duplicate category labels.')
    return Dimension(name, tuple(categories), multi=bool(raw.get('multi')), single_column=column,
                     unbound_categories=tuple(unbound_categories))


def _dataset(key: str, raw: Any) -> DatasetBinding:
    where = f'datasets.{key}'
    if not isinstance(raw, dict):
        raise BindingsError(f'{where}: must be a mapping.')
    required = {'superset_dataset_id', 'label', 'grain', 'synthetic', 'sensitivity', 'min_cell_size', 'site',
                'columns', 'roles', 'geography', 'dimensions'}
    missing = required - set(raw)
    if missing:
        raise BindingsError(f'{where}: missing {sorted(missing)}.')
    extra = set(raw) - required - {'reference_date', 'notes'}
    if extra:
        raise BindingsError(f'{where}: unknown keys {sorted(extra)}.')
    if raw['grain'] != 'person':
        raise BindingsError(f'{where}: the HDSS template counts persons; grain must be person.')
    if raw['sensitivity'] not in SENSITIVITY:
        raise BindingsError(f'{where}: sensitivity must be one of {sorted(SENSITIVITY)}.')
    if type(raw['min_cell_size']) is not int or raw['min_cell_size'] < 0:
        raise BindingsError(f'{where}: min_cell_size must be a non-negative integer.')
    if type(raw['superset_dataset_id']) is not int or raw['superset_dataset_id'] < 1:
        raise BindingsError(f'{where}: superset_dataset_id must be a positive integer.')
    columns = {str(k): v for k, v in (raw['columns'] or {}).items()}
    bad = {k: v for k, v in columns.items() if v not in COLUMN_ROLES}
    if bad:
        raise BindingsError(f'{where}.columns: unknown roles {bad}.')

    def need(column: Any, role: str, at: str) -> str:
        if not isinstance(column, str) or columns.get(column) != role:
            raise BindingsError(f'{at}: {column!r} must be a column declared with role {role}.')
        return column

    site = raw['site']
    if not isinstance(site, dict) or not isinstance(site.get('value'), str) or not site['value'].strip():
        raise BindingsError(f'{where}.site: needs a value (and a column unless the dataset is one site).')
    site_column = need(site['column'], 'site', f'{where}.site') if site.get('column') else None

    roles: dict[str, str | Unbound] = {}
    for role, required_role in ROLES.items():
        value = (raw['roles'] or {}).get(role)
        unbound = _unbound(value, f'{where}.roles.{role}')
        roles[role] = unbound or (need(value, required_role, f'{where}.roles.{role}') if value is not None
                                  else Unbound('no_source', 'Not registered.'))
    if isinstance(roles['household_id'], Unbound):
        raise BindingsError(f'{where}: the household key is required (households, assets and the gauge use it).')
    unknown_roles = set(raw['roles'] or {}) - set(ROLES)
    if unknown_roles:
        raise BindingsError(f'{where}.roles: unknown roles {sorted(unknown_roles)}.')

    geography: dict[str, str | Unbound] = {}
    for level in GEOGRAPHY_LEVELS:
        value = (raw['geography'] or {}).get(level)
        unbound = _unbound(value, f'{where}.geography.{level}')
        geography[level] = unbound or (need(value, 'geography', f'{where}.geography.{level}') if value is not None
                                       else Unbound('no_source', 'Not registered.'))

    dimensions: dict[str, Dimension | Unbound] = {}
    for name in DIMENSIONS:
        value = (raw['dimensions'] or {}).get(name)
        dimensions[name] = (_dimension(name, value, columns, f'{where}.dimensions.{name}') if value is not None
                            else Unbound('no_source', 'Not registered.'))
    unknown_dims = set(raw['dimensions'] or {}) - set(DIMENSIONS)
    if unknown_dims:
        raise BindingsError(f'{where}.dimensions: unknown dimensions {sorted(unknown_dims)}.')
    for name in ('age_band', 'event'):
        dim = dimensions[name]
        expected = {'age_band': ['0-4'], 'event': ['Births', 'Deaths', 'In-migration', 'Out-migration']}[name]
        if isinstance(dim, Dimension) and not set(expected) <= set(dim.labels) | {u[0] for u in dim.unbound_categories}:
            raise BindingsError(f'{where}.dimensions.{name}: the KPI cards need categories {expected}.')
    reference = raw.get('reference_date')
    if reference is not None:
        from datetime import date
        try:
            date.fromisoformat(str(reference))
        except ValueError as exc:
            raise BindingsError(f'{where}.reference_date: use YYYY-MM-DD.') from exc
    return DatasetBinding(
        key=key, superset_dataset_id=raw['superset_dataset_id'], label=str(raw['label']), grain='person',
        synthetic=bool(raw['synthetic']), sensitivity=raw['sensitivity'], min_cell_size=raw['min_cell_size'],
        site_column=site_column, site_value=site['value'].strip(), columns=columns, roles=roles,
        geography=geography, dimensions=dimensions, reference_date=str(reference) if reference else None,
        notes=list(raw.get('notes') or []))


def parse_bindings(text: str) -> dict[str, DatasetBinding]:
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise BindingsError(f'bindings.yaml is not valid YAML: {exc}') from exc
    if raw.get('version') != 1 or not isinstance(raw.get('datasets'), dict):
        raise BindingsError('bindings.yaml needs version: 1 and a datasets mapping.')
    out = {str(k): _dataset(str(k), v) for k, v in raw['datasets'].items()}
    ids = [b.superset_dataset_id for b in out.values()]
    if len(ids) != len(set(ids)):
        raise BindingsError('Each Superset dataset may be registered once.')
    return out


_lock = threading.Lock()
_cache: tuple[float, dict[str, DatasetBinding]] | None = None


def load_bindings(path: Path | None = None) -> dict[str, DatasetBinding]:
    """Parsed bindings, re-read when the file's mtime changes (no container restart needed)."""
    global _cache
    path = path or BINDINGS_PATH
    if path != BINDINGS_PATH:
        return parse_bindings(path.read_text(encoding='utf-8'))
    mtime = os.stat(path).st_mtime
    with _lock:
        if _cache is None or _cache[0] != mtime:
            _cache = (mtime, parse_bindings(path.read_text(encoding='utf-8')))
        return _cache[1]


def binding_for_dataset(dataset_id: int) -> DatasetBinding | None:
    return next((b for b in load_bindings().values() if b.superset_dataset_id == dataset_id), None)
