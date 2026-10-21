"""Server-side aggregation for the HDSS template (the page's only data source).

The browser sends a filter state; this module checks every value against the registered dimension
categories and geography, plans one GROUP BY per card through Superset's /api/v1/chart/data (run with
the viewer's own session, so Superset's dataset permissions apply), and returns per-card counts.

The filter model is the approved design's, exactly:
  * a person passes when ALL active filters match; household attributes are columns on the person's
    row (bindings declare only datasets where they repeat per member);
  * OWN-DIMENSION RULE: a card that is itself a filter ignores its own dimension, so it keeps showing
    every alternative (the browser highlights the selected one);
  * trend lines cover the displayed time window (the active time filter, else first record to the
    reference date) with the card's own filters, bucketed by day / week / month / year.

Nothing here returns a row: only counts per category, geography names, and per-village centroids.
Counts between 1 and min_cell_size-1 are replaced by null and reported as suppressed ("<n").
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from lifedata_ai.hdss.registry import GEOGRAPHY_LEVELS, DatasetBinding, Dimension

PERIODS = {'All': None, 'Last 6 months': 182, 'Last 3 months': 91}
COUNT = {'expressionType': 'SQL', 'sqlExpression': 'COUNT(*)', 'label': 'n'}
DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
ROW_LIMIT = 10000
#: Dimensions whose card is a plain category breakdown (age/sex/event/asset have their own cards).
CATEGORY_CARDS = ('fuel', 'water', 'education', 'occupation', 'marital', 'migration', 'bednet', 'roof', 'floor', 'toilet')


class StateError(ValueError):
    """The browser sent a filter value the dashboard does not allow."""


#: The Time range dropdown. Calendar periods relative to the dataset's reference date (weeks start Monday,
#: as Postgres DATE_TRUNC('week') does); "Previous years" is everything before 1 January of that year.
RANGES = ('All', 'This week', 'Last week', 'This month', 'Last month', 'This quarter', 'Last quarter',
          'This year', 'Previous years')


def _month_end(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def range_bounds(name: str, ref: date) -> tuple[date | None, date | None]:
    """Inclusive (first day, last day) of a Time range option; None = open."""
    monday = ref - timedelta(days=ref.weekday())
    month = ref.replace(day=1)
    quarter = date(ref.year, 3 * ((ref.month - 1) // 3) + 1, 1)
    previous_quarter = (quarter - timedelta(days=1)).replace(day=1)
    previous_quarter = date(previous_quarter.year, 3 * ((previous_quarter.month - 1) // 3) + 1, 1)
    return {
        'All': (None, None),
        'This week': (monday, monday + timedelta(days=6)),
        'Last week': (monday - timedelta(days=7), monday - timedelta(days=1)),
        'This month': (month, _month_end(ref)),
        'Last month': ((month - timedelta(days=1)).replace(day=1), month - timedelta(days=1)),
        'This quarter': (quarter, _month_end(date(quarter.year, quarter.month + 2, 1))),
        'Last quarter': (previous_quarter, quarter - timedelta(days=1)),
        'This year': (date(ref.year, 1, 1), date(ref.year, 12, 31)),
        'Previous years': (None, date(ref.year - 1, 12, 31)),
    }[name]


def trunc(d: date, unit: str) -> date:
    return {'day': d, 'week': d - timedelta(days=d.weekday()), 'month': d.replace(day=1), 'year': date(d.year, 1, 1)}[unit]


def step(d: date, unit: str) -> date:
    if unit == 'day':
        return d + timedelta(days=1)
    if unit == 'week':
        return d + timedelta(days=7)
    if unit == 'month':
        return (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return date(d.year + 1, 1, 1)


def spark_unit(lo: date, hi: date) -> str:
    """Bucket size for a trend window: days up to a month, weeks up to a quarter, months up to 3 years."""
    days = (hi - lo).days + 1
    return 'day' if days <= 31 else 'week' if days <= 124 else 'month' if days <= 1100 else 'year'


@dataclass
class FilterState:
    geo: dict[str, str] = field(default_factory=dict)
    period: str = 'All'
    range: str = 'All'
    date_from: str = ''
    date_to: str = ''
    dims: dict[str, str] = field(default_factory=dict)

    def canonical(self) -> dict[str, Any]:
        return {'geo': dict(sorted(self.geo.items())), 'period': self.period, 'range': self.range,
                'from': self.date_from, 'to': self.date_to, 'dims': dict(sorted(self.dims.items()))}

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.canonical(), sort_keys=True).encode()).hexdigest()


def reference_day(binding: DatasetBinding) -> date:
    return date.fromisoformat(binding.reference_date) if binding.reference_date else datetime.now(timezone.utc).date()


def parse_state(raw: Any, binding: DatasetBinding, hierarchy: list[dict[str, str]], filters: list[str]) -> FilterState:
    """Check a browser filter state. Every key must be an enabled filter and every value a known option."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise StateError('The filter state must be an object.')
    unknown = set(raw) - {'geo', 'period', 'range', 'from', 'to', 'dims'}
    if unknown:
        raise StateError(f'Unknown filter keys: {sorted(unknown)}.')
    state = FilterState()
    geo = raw.get('geo') or {}
    if not isinstance(geo, dict):
        raise StateError('geo must be an object.')
    for level, value in geo.items():
        if level not in GEOGRAPHY_LEVELS or level not in filters or not binding.level(level):
            raise StateError(f'{level!r} is not a geography filter on this dashboard.')
        if value in (None, '', 'All'):
            continue
        if not isinstance(value, str) or value not in {row.get(level) for row in hierarchy}:
            raise StateError(f'{level} {value!r} is not in this dataset.')
        state.geo[level] = value
    time_enabled = bool(binding.role('record_date'))
    period = raw.get('period') or 'All'
    if period not in PERIODS or (period != 'All' and not (time_enabled and 'time_period' in filters)):
        raise StateError(f'Time period {period!r} is not available.')
    state.period = period
    time_range = raw.get('range') or 'All'
    if time_range not in RANGES or (time_range != 'All' and not (time_enabled and 'time_range' in filters)):
        raise StateError(f'Time range {time_range!r} is not available.')
    state.range = time_range
    for key, attr in (('from', 'date_from'), ('to', 'date_to')):
        value = raw.get(key) or ''
        if value:
            if not (time_enabled and 'time_range' in filters):
                raise StateError('The time range filter is not available on this dashboard.')
            if not isinstance(value, str) or not DATE.match(value):
                raise StateError(f'{key} must be a YYYY-MM-DD date.')
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise StateError(f'{key} is not a real date.') from exc
        setattr(state, attr, value)
    dims = raw.get('dims') or {}
    if not isinstance(dims, dict):
        raise StateError('dims must be an object.')
    for name, label in dims.items():
        if label in (None, ''):
            continue
        dim = binding.dimension(name) if name in filters else None
        if dim is None:
            raise StateError(f'{name!r} is not a filter on this dashboard.')
        if not isinstance(label, str) or dim.category(label) is None:
            raise StateError(f'{label!r} is not a {name} category.')
        state.dims[name] = label
    return state


class Planner:
    """Builds the query objects for one state and turns grouped results into card counts."""

    def __init__(self, binding: DatasetBinding, state: FilterState, slots: list[str], profile: dict[str, Any] | None = None):
        self.b, self.s, self.slots = binding, state, set(slots)
        self.ref = reference_day(binding)
        self.profile = profile or {}
        self.queries: list[tuple[str, dict[str, Any]]] = []

    # ------------------------------------------------------------------ filters
    def site_filters(self) -> list[dict[str, Any]]:
        return [{'col': self.b.site_column, 'op': '==', 'val': self.b.site_value}] if self.b.site_column else []

    def time_bounds(self) -> tuple[str | None, str | None]:
        """The active time window: Time period, Time range and explicit dates all apply (intersection)."""
        lows, highs = [self.s.date_from] if self.s.date_from else [], [self.s.date_to] if self.s.date_to else []
        days = PERIODS[self.s.period]
        if days is not None:
            lows.append((self.ref - timedelta(days=days)).isoformat())
        range_lo, range_hi = range_bounds(self.s.range, self.ref)
        if range_lo:
            lows.append(range_lo.isoformat())
        if range_hi:
            highs.append(range_hi.isoformat())
        return (max(lows) if lows else None), (min(highs) if highs else None)

    def filters(self, skip: set[str] = frozenset(), use_time: bool = True) -> list[dict[str, Any]]:
        out = self.site_filters()
        for level, value in self.s.geo.items():
            out.append({'col': self.b.level(level), 'op': '==', 'val': value})
        column = self.b.role('record_date')
        if use_time and column:
            lo, hi = self.time_bounds()
            if lo:
                out.append({'col': column, 'op': '>=', 'val': lo})
            if hi:
                out.append({'col': column, 'op': '<=', 'val': hi})
        for name, label in self.s.dims.items():
            if name not in skip:
                out.extend(self.b.dimension(name).category(label).superset_filters())
        return out

    # ------------------------------------------------------------------ plan
    def add(self, name: str, columns: list[Any], metrics: list[dict[str, Any]], filters: list[dict[str, Any]]):
        self.queries.append((name, {'columns': columns, 'metrics': metrics, 'filters': filters,
                                    'time_range': 'No filter', 'row_limit': ROW_LIMIT, 'orderby': [],
                                    'extras': {'having': '', 'where': ''}}))

    def plan(self) -> list[tuple[str, dict[str, Any]]]:
        b = self.b
        household = {'expressionType': 'SIMPLE', 'aggregate': 'COUNT_DISTINCT',
                     'column': {'column_name': b.role('household_id')}, 'label': 'hh'}
        village = b.level('village')
        total_metrics = [COUNT, household]
        if village:
            total_metrics.append({'expressionType': 'SIMPLE', 'aggregate': 'COUNT_DISTINCT',
                                  'column': {'column_name': village}, 'label': 'villages'})
        # overall totals and the geography list do not depend on the filters: see profile().
        self.add('totals', [], total_metrics, self.filters())
        levels = [b.level(level) for level in b.bound_levels]
        age, sex, event, asset = (b.dimension(k) for k in ('age_band', 'sex', 'event', 'asset'))
        if event:
            self.add('event', event.columns, [COUNT], self.filters({'event'}))
        if age:
            self.add('age', age.columns, [COUNT], self.filters({'age_band'}))
        if age and sex:
            self.add('age_sex', age.columns + sex.columns, [COUNT], self.filters({'age_band', 'sex'}))
        for name in CATEGORY_CARDS:
            dim = b.dimension(name)
            if dim:
                self.add(name, dim.columns, [COUNT], self.filters({name}))
        if asset:
            self.add('asset', asset.columns, [COUNT], self.filters({'asset'}))
        lat, lon = b.role('latitude'), b.role('longitude')
        if village and lat and lon:
            self.add('map', levels, [COUNT, _avg(lat, 'lat'), _avg(lon, 'lon')], self.filters())
        date_column = b.role('record_date')
        if date_column:
            # Trend lines cover exactly the displayed time window, with the KPI's own filters, so each line
            # adds up to the number on its card.
            unit = self.spark_unit()
            bucket = {'expressionType': 'SQL', 'label': 'bucket',
                      'sqlExpression': f'DATE_TRUNC(\'{unit}\', "{date_column}")'}
            if event:
                self.add('spark_event', [bucket] + event.columns, [COUNT], self.filters({'event'}))
            if age:
                under_five = age.category('0-4')
                self.add('spark_u5', [bucket], [COUNT], self.filters({'age_band'}) + under_five.superset_filters())
        return self.queries

    def spark_window(self) -> tuple[date, date]:
        """The displayed timeline: the active time filter, else first record to the reference date.
        A current period ("This month") stops at the reference date instead of showing empty future days."""
        lo, hi = self.time_bounds()
        first = date.fromisoformat(lo) if lo else (date.fromisoformat(self.profile['date_min'])
                                                    if self.profile.get('date_min') else self.ref.replace(day=1, year=self.ref.year - 1))
        last = min(date.fromisoformat(hi), self.ref) if hi else self.ref
        if self.profile.get('date_max') and not hi:
            last = max(last, date.fromisoformat(self.profile['date_max']))
        return first, max(first, last)

    def spark_unit(self) -> str:
        return spark_unit(*self.spark_window())

    def buckets(self) -> list[date]:
        first, last = self.spark_window()
        unit = self.spark_unit()
        out, d = [], trunc(first, unit)
        while d <= last:
            out.append(d)
            d = step(d, unit)
        return out

    # ------------------------------------------------------------------ assemble
    def assemble(self, results: dict[str, list[dict[str, Any]]], profile: dict[str, Any]) -> dict[str, Any]:
        b, k = self.b, self.b.min_cell_size
        suppressed = {'any': False}

        def cell(n: Any) -> int | None:
            n = int(n or 0)
            if 0 < n < k:
                suppressed['any'] = True
                return None
            return n

        def counts(dim: Dimension, rows: list[dict[str, Any]]) -> list[int | None]:
            totals = {label: 0 for label in dim.labels}
            for row in rows:
                for label in dim.assign(row):
                    totals[label] += int(row.get('n') or 0)
            return [cell(totals[label]) for label in dim.labels]

        one = lambda name: (results.get(name) or [{}])[0]
        totals = one('totals')
        out: dict[str, Any] = {
            'overall': dict(profile['overall']),
            'totals': {'persons': cell(totals.get('n')), 'households': cell(totals.get('hh')),
                       'villages': int(totals['villages']) if 'villages' in totals else None},
            'cards': {}, 'spark': {}, 'map': [],
        }
        for name in ('event', 'age', 'asset', *CATEGORY_CARDS):
            source = 'age_band' if name == 'age' else name
            dim = b.dimension(source)
            if dim and name in results:
                out['cards'][name] = {'labels': dim.labels, 'counts': counts(dim, results[name])}
        age, sex = b.dimension('age_band'), b.dimension('sex')
        if 'age_sex' in results and age and sex:
            grid = {s: {a: 0 for a in age.labels} for s in sex.labels}
            for row in results['age_sex']:
                for a in age.assign(row):
                    for s in sex.assign(row):
                        grid[s][a] += int(row.get('n') or 0)
            out['cards']['age_sex'] = {'bands': age.labels, 'sexes': sex.labels,
                                       'counts': {s: [cell(grid[s][a]) for a in age.labels] for s in sex.labels}}
        levels = b.bound_levels
        if levels:
            out['geography'] = {'levels': levels, 'paths': profile['paths']}
        if 'map' in results:
            for row in results['map']:
                n = cell(row.get('n'))
                path = {level: row.get(b.level(level)) for level in levels}
                if n is None or row.get('lat') is None or row.get('lon') is None:
                    continue
                out['map'].append({'path': path, 'n': n, 'lat': float(row['lat']), 'lon': float(row['lon'])})
        if 'spark_event' in results or 'spark_u5' in results:
            buckets = self.buckets()
            index = {d: i for i, d in enumerate(buckets)}
            out['spark'] = {'unit': self.spark_unit(), 'buckets': [d.isoformat() for d in buckets]}
            event = b.dimension('event')
            if event and 'spark_event' in results:
                series = {label: [0] * len(buckets) for label in event.labels}
                for row in results['spark_event']:
                    i = index.get(_day(row.get('bucket')))
                    if i is None:
                        continue
                    for label in event.assign(row):
                        series[label][i] += int(row.get('n') or 0)
                out['spark']['event'] = {label: [cell(v) for v in values] for label, values in series.items()}
            if 'spark_u5' in results:
                series = [0] * len(buckets)
                for row in results['spark_u5']:
                    i = index.get(_day(row.get('bucket')))
                    if i is not None:
                        series[i] += int(row.get('n') or 0)
                out['spark']['under_five'] = [cell(v) for v in series]
        out['suppression'] = {'min_cell_size': k, 'applied': suppressed['any']}
        return out


def _avg(column: str, label: str) -> dict[str, Any]:
    return {'expressionType': 'SIMPLE', 'aggregate': 'AVG', 'column': {'column_name': column}, 'label': label}


def _sort_key(path: tuple) -> tuple:
    return tuple('' if v is None else str(v) for v in path)


def _day(value: Any) -> date | None:
    """A bucket/date value from Superset (epoch milliseconds or ISO text) as a date."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date()
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


Executor = Callable[[int, list[dict[str, Any]]], list[list[dict[str, Any]]]]

#: Upper bound on the groups one merged card query may return (product of distinct values + 1 per column).
MERGE_CAP = 2000
#: Card queries that count persons per category and may share one GROUP BY (their sums stay exact).
MERGEABLE = ('event', 'age', 'age_sex', 'asset', *CATEGORY_CARDS)


def profile(binding: DatasetBinding, execute: Executor) -> dict[str, Any]:
    """What does not depend on the filters, fetched once per dashboard and cached by the caller:
    overall totals (the "of N" denominators), geography paths (names only, for options and
    validation) and distinct counts per dimension column (to bound merged queries)."""
    planner = Planner(binding, FilterState(), [])
    site = planner.site_filters()
    household = {'expressionType': 'SIMPLE', 'aggregate': 'COUNT_DISTINCT',
                 'column': {'column_name': binding.role('household_id')}, 'label': 'hh'}
    dims = sorted({c for d in binding.dimensions.values() if isinstance(d, Dimension) for c in d.columns})
    distinct = [{'expressionType': 'SIMPLE', 'aggregate': 'COUNT_DISTINCT', 'column': {'column_name': c}, 'label': f'd{i}'}
                for i, c in enumerate(dims)]
    date_column = binding.role('record_date')
    if date_column:
        distinct += [{'expressionType': 'SIMPLE', 'aggregate': agg, 'column': {'column_name': date_column}, 'label': label}
                     for agg, label in (('MIN', 'date_min'), ('MAX', 'date_max'))]
    planner.add('overall', [], [COUNT, household, *distinct], site)
    levels = [binding.level(level) for level in binding.bound_levels]
    if levels:
        planner.add('geography', levels, [COUNT], site)
    rows = execute(binding.superset_dataset_id, [q for _, q in planner.queries])
    overall = (rows[0] or [{}])[0]
    paths = set()
    if levels:
        if len(rows[1]) >= ROW_LIMIT:
            raise RuntimeError('The geography list hit the group limit; refusing a partial list.')
        paths = {tuple(row.get(c) for c in levels) for row in rows[1]}
    span = {k: (d.isoformat() if (d := _day(overall.get(k))) else None) for k in ('date_min', 'date_max')}
    return {'overall': {'persons': int(overall.get('n') or 0), 'households': int(overall.get('hh') or 0)}, **span,
            'distinct': {c: int(overall.get(f'd{i}') or 0) for i, c in enumerate(dims)},
            'paths': [dict(zip(binding.bound_levels, p)) for p in sorted(paths, key=_sort_key) if all(v is not None for v in p)]}


def merge(plan: list[tuple[str, dict[str, Any]]], distinct: dict[str, int]) -> list[tuple[list[str], dict[str, Any]]]:
    """Fold COUNT-only card queries that share identical filters into one GROUP BY over the union of
    their columns, while the possible number of groups stays under MERGE_CAP. Every logical query still
    sums `n` over only its own columns, so its counts are unchanged. Returns (names, physical query)."""
    size = lambda cols: math.prod(distinct.get(c, ROW_LIMIT) + 1 for c in cols)
    out: list[tuple[list[str], dict[str, Any]]] = []
    open_groups: dict[str, list[tuple[list[str], dict[str, Any]]]] = {}
    for name, query in plan:
        if name not in MERGEABLE:
            out.append(([name], query))
            continue
        key = json.dumps(query['filters'], sort_keys=True)
        for names, physical in open_groups.setdefault(key, []):
            union = list(dict.fromkeys(physical['columns'] + query['columns']))
            if size(union) <= MERGE_CAP:
                physical['columns'] = union
                names.append(name)
                break
        else:
            entry = ([name], {**query, 'columns': list(query['columns'])})
            open_groups[key].append(entry)
            out.append(entry)
    return out


def run(binding: DatasetBinding, state: FilterState, slots: list[str], execute: Executor,
        prof: dict[str, Any] | None = None) -> dict[str, Any]:
    """Plan, merge, execute (one round trip) and assemble. `execute(dataset_id, queries)` returns rows per query."""
    prof = prof or profile(binding, execute)
    planner = Planner(binding, state, slots, prof)
    physical = merge(planner.plan(), prof['distinct'])
    rows = execute(binding.superset_dataset_id, [q for _, q in physical])
    if len(rows) != len(physical):
        raise RuntimeError('Superset returned a different number of results than queries sent.')
    results: dict[str, list[dict[str, Any]]] = {}
    for (names, _), data in zip(physical, rows):
        if len(data) >= ROW_LIMIT:
            raise RuntimeError(f'The {names[0]} breakdown hit the {ROW_LIMIT}-group limit; refusing a partial count.')
        for name in names:
            results[name] = data
    return planner.assemble(results, prof)


def superset_executor(client) -> Executor:
    """Run every query in one /api/v1/chart/data call with the viewer's session."""
    def execute(dataset_id: int, queries: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        payload = client.chart_data({'datasource': {'id': dataset_id, 'type': 'table'}, 'force': False,
                                     'queries': queries, 'result_format': 'json', 'result_type': 'full'})
        results = payload.get('result') or []
        for result in results:
            if result.get('error'):
                raise RuntimeError(f"Superset could not run an aggregate: {result['error']}")
        return [result.get('data') or [] for result in results]
    return execute


# ---------------------------------------------------------------------------- in-memory cube (fast clicks)
#: A dashboard whose dataset collapses to at most this many category combinations is answered from a
#: server-side cube: one grouped query when the page first loads (cached per dashboard), then every
#: click is computed in memory in milliseconds instead of a Superset round trip (~1 s per query on
#: sandbox). The cube holds COUNTS per combination and never leaves the server; responses are the same
#: per-card aggregates as the Superset path. Larger datasets keep querying Superset per filter state.
CUBE_LIMIT = 50000
_SUM_LAT, _SUM_LON = '__sum_lat', '__sum_lon'


def cube_columns(binding: DatasetBinding) -> list[str]:
    cols = {c for d in binding.dimensions.values() if isinstance(d, Dimension) for c in d.columns}
    cols |= {binding.level(level) for level in binding.bound_levels}
    cols.add(binding.role('household_id'))
    if binding.role('record_date'):
        cols.add(binding.role('record_date'))
    return sorted(cols)


def load_cube(binding: DatasetBinding, execute: Executor) -> list[dict[str, Any]] | None:
    """Counts per combination of every column the cards use (site rows only), or None when too large."""
    planner = Planner(binding, FilterState(), [])
    metrics = [COUNT]
    lat, lon = binding.role('latitude'), binding.role('longitude')
    if lat and lon:
        metrics += [{'expressionType': 'SIMPLE', 'aggregate': 'SUM', 'column': {'column_name': lat}, 'label': _SUM_LAT},
                    {'expressionType': 'SIMPLE', 'aggregate': 'SUM', 'column': {'column_name': lon}, 'label': _SUM_LON}]
    planner.add('cube', cube_columns(binding), metrics, planner.site_filters())
    query = dict(planner.queries[0][1], row_limit=CUBE_LIMIT + 1)
    rows = execute(binding.superset_dataset_id, [query])[0]
    if len(rows) > CUBE_LIMIT:
        return None
    date_column = binding.role('record_date')
    for row in rows:
        if binding.site_column:
            row[binding.site_column] = binding.site_value
        if date_column and row.get(date_column) is not None:
            row[date_column] = _iso_day(row[date_column])
    return rows


def _iso_day(value: Any) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date().isoformat()
    return str(value)[:10]


def _compare(value: Any, op: str, target: Any) -> bool:
    if op == 'IN':
        return value is not None and str(value) in {str(t) for t in target}
    if op == '==':
        return value is not None and str(value) == str(target)
    if value is None:
        return False
    try:
        if isinstance(target, str):
            value = str(value)
        elif not isinstance(value, (int, float)):
            value = float(value)
        return {'>=': value >= target, '<=': value <= target, '<': value < target, '>': value > target}[op]
    except (TypeError, ValueError):
        return False


def cube_executor(rows: list[dict[str, Any]], binding: DatasetBinding) -> Executor:
    """Answers the planner's query objects from cube rows, with Superset's semantics for our metrics."""
    date_column = binding.role('record_date')

    def group_key(column: Any, row: dict[str, Any]) -> tuple[str, Any]:
        if isinstance(column, str):
            return column, row.get(column)
        day = row.get(date_column)  # the only expression column the planner emits: DATE_TRUNC('<unit>', date)
        if day is None:
            return column['label'], None
        d = trunc(date.fromisoformat(day), column['sqlExpression'].split("'")[1])
        return column['label'], int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)

    def one(query: dict[str, Any]) -> list[dict[str, Any]]:
        matched = [r for r in rows if all(_compare(r.get(f['col']), f['op'], f['val']) for f in query['filters'])]
        groups: dict[tuple, list[dict[str, Any]]] = {}
        for r in matched:
            groups.setdefault(tuple(group_key(c, r) for c in query['columns']), []).append(r)
        if not query['columns'] and not groups:
            groups[()] = []
        out = []
        for key, members in groups.items():
            row = dict(key)
            n = sum(int(m.get('n') or 0) for m in members)
            for metric in query['metrics']:
                label = metric['label']
                if metric.get('expressionType') == 'SQL':
                    row[label] = n
                elif metric['aggregate'] == 'COUNT_DISTINCT':
                    col = metric['column']['column_name']
                    row[label] = len({m.get(col) for m in members if m.get(col) is not None})
                elif metric['aggregate'] == 'AVG':
                    total = _SUM_LAT if metric['label'] == 'lat' else _SUM_LON
                    # as SQL AVG: rows without a coordinate are left out, never counted as 0
                    known = [m for m in members if m.get(total) is not None]
                    k = sum(int(m.get('n') or 0) for m in known)
                    row[label] = sum(float(m[total]) for m in known) / k if k else None
            out.append(row)
        return out

    return lambda dataset_id, queries: [one(q) for q in queries]
