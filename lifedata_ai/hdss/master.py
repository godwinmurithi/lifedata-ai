"""The Master dashboard template (hdss-master): three tabs, ONE shared filter state.

Registry: master_bindings.yaml (scoped to this template). Data: each registered entity (villages, households,
persons, events, sites, site_weeks, site_lab, signals, clusters) is loaded once per dashboard as GROUPED
COUNTS through Superset with the viewer's session, kept server-side, and every filter state is answered
from it by the approved design's filter model (docs/design/master/design_master_dashboard.dc.html):

  scope(skip): villages = geography AND tier; any person/household filter keeps villages with at least
  one qualifying household; an event-type filter keeps villages with that event in the period; any site
  filter keeps villages of qualifying sites. Each chart computes inside scope(its own dimension skipped).

Only per-card aggregates leave the server (counts per category, per-village and per-site summaries);
person- and household-level rows never do. Counts below a source's min_cell_size come back as null.
"""
from __future__ import annotations

import math
import os
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from lifedata_ai.hdss.engine import COUNT, StateError, _day, range_bounds, RANGES
from lifedata_ai.hdss.registry import BindingsError
from lifedata_ai.hdss.spec import SpecError

MASTER_PATH = Path(__file__).resolve().parent / 'master_bindings.yaml'
TEMPLATE = PRESET = 'hdss-master'
ALL_LEVELS = ('country', 'site', 'district', 'county', 'subcounty', 'parish', 'village')
ENTITIES = ('villages', 'households', 'persons', 'events', 'sites', 'site_weeks', 'site_lab', 'signals', 'clusters')
COLUMN_ROLES = {'key', 'household_key', 'site', 'geography', 'coordinate', 'dimension', 'measure', 'temporal', 'label',
                'personal', 'internal'}
PRIVATE = {'personal', 'internal'}
LOAD_LIMIT = 50000
#: Filters of the shared state, with the design's names in comments.
DIM_FILTERS = ('tier', 'event_type', 'age_band', 'sex', 'housing', 'water', 'bednet', 'syndrome', 'pathogen',
               'site_status', 'signal_severity')


class MasterBindingsError(BindingsError):
    pass


# ============================================================================ registry
def _unbound(raw: Any) -> dict[str, str] | None:
    if isinstance(raw, dict) and len(raw) == 1 and set(raw) & {'needs_definition', 'no_source'}:
        kind = next(iter(raw))
        return {'status': kind, 'reason': str(raw[kind])}
    return None


def _categories(raw: Any, where: str) -> list[tuple[str, list[str]]]:
    if isinstance(raw, list):
        return [(str(v), [str(v)]) for v in raw]
    if isinstance(raw, dict):
        return [(str(k), [str(x) for x in (v if isinstance(v, list) else [v])]) for k, v in raw.items()]
    raise MasterBindingsError(f'{where}: categories must be a list or a mapping.')


def _check_columns(entity: dict[str, Any], names: list[str], where: str, role: str | None = None) -> None:
    columns = entity.get('columns') or {}
    for name in names:
        r = columns.get(name)
        if r is None:
            raise MasterBindingsError(f'{where}: column {name!r} is not declared.')
        if r in PRIVATE:
            raise MasterBindingsError(f'{where}: column {name!r} is {r} and cannot be used.')
        if role and r != role:
            raise MasterBindingsError(f'{where}: column {name!r} must have role {role}, not {r}.')


def parse_master(text: str) -> dict[str, Any]:
    raw = yaml.safe_load(text) or {}
    if raw.get('version') != 1 or not isinstance(raw.get('sources'), dict):
        raise MasterBindingsError('master_bindings.yaml needs version: 1 and sources.')
    cycles = []
    for c in raw.get('cycles') or []:
        cycles.append({'label': str(c['label']), 'from': date.fromisoformat(str(c['from'])), 'to': date.fromisoformat(str(c['to']))})
    out = {'countries': {str(k): str(v) for k, v in (raw.get('countries') or {}).items()}, 'cycles': cycles, 'sources': {}}
    for key, src in raw['sources'].items():
        where = f'sources.{key}'
        for req in ('label', 'synthetic', 'sensitivity', 'min_cell_size', 'geography', 'villages', 'persons'):
            if req not in src:
                raise MasterBindingsError(f'{where}: missing {req}.')
        if src['sensitivity'] != 'aggregate_only':
            raise MasterBindingsError(f'{where}: sensitivity must be aggregate_only.')
        geo, drill = list(src['geography']), list(src.get('drill') or [])
        if any(g not in ALL_LEVELS for g in geo + drill) or set(geo) & set(drill):
            raise MasterBindingsError(f'{where}.geography/drill: levels must be among {ALL_LEVELS}, each listed once.')
        ents: dict[str, Any] = {}
        for name in ENTITIES:
            e = src.get(name)
            if e is None:
                ents[name] = {'status': 'no_source', 'reason': 'Not registered.'}
                continue
            unbound = _unbound(e)
            if unbound:
                ents[name] = unbound
                continue
            ew = f'{where}.{name}'
            if type(e.get('dataset')) is not int:
                raise MasterBindingsError(f'{ew}: dataset must be a Superset dataset id.')
            bad = {c: r for c, r in (e.get('columns') or {}).items() if r not in COLUMN_ROLES}
            if bad:
                raise MasterBindingsError(f'{ew}.columns: unknown roles {bad}.')
            for ref in ('key', 'village', 'household', 'site', 'date', 'week_label', 'count'):
                if ref in e:
                    cols = e[ref] if isinstance(e[ref], list) else [e[ref]]
                    _check_columns(e, cols, f'{ew}.{ref}')
            for dname, d in (e.get('dimensions') or {}).items():
                if _unbound(d):
                    continue
                _check_columns(e, [d['column']], f'{ew}.dimensions.{dname}', 'dimension')
                d['_cats'] = _categories(d['categories'], f'{ew}.dimensions.{dname}')
            for dname in ('types', 'status', 'syndromes', 'pathogens', 'severity'):
                if dname in e:
                    _check_columns(e, [e[dname]['column']], f'{ew}.{dname}', 'dimension')
                    e[dname]['_cats'] = _categories(e[dname]['categories'], f'{ew}.{dname}')
            for role, col in (e.get('roles') or {}).items():
                if isinstance(col, str):
                    _check_columns(e, [col], f'{ew}.roles.{role}')
            for level, col in (e.get('levels') or {}).items():
                if isinstance(col, str):
                    _check_columns(e, [col], f'{ew}.levels.{level}')
                elif isinstance(col, dict) and 'site_map' in col:
                    _check_columns(e, [col['site_map']], f'{ew}.levels.{level}')
                elif isinstance(col, dict) and 'from' in col and isinstance(col.get('map'), dict):
                    _check_columns(e, [col['from']], f'{ew}.levels.{level}')
                    col['map'] = {str(k): str(v) for k, v in col['map'].items()}
                else:
                    raise MasterBindingsError(f'{ew}.levels.{level}: a column, {{site_map: column}} or {{from: column, map: {{...}}}}.')
            if e.get('resident'):
                _check_columns(e, [e['resident']['column']], f'{ew}.resident')
            ents[name] = e
        if 'status' in ents['villages'] or 'status' in ents['persons']:
            raise MasterBindingsError(f'{where}: villages and persons must be bound.')
        missing = [lvl for lvl in geo + drill if lvl not in (ents['villages'].get('levels') or {})]
        if missing:
            raise MasterBindingsError(f'{where}: villages.levels has no {missing}.')
        out['sources'][key] = {'key': key, 'label': src['label'], 'synthetic': bool(src['synthetic']),
                               'min_cell_size': int(src['min_cell_size']), 'reference_date': src.get('reference_date'),
                               'geography': geo, 'drill': drill, **ents}
    return out


_lock = threading.Lock()
_cache: tuple[float, dict[str, Any]] | None = None


def load_master() -> dict[str, Any]:
    global _cache
    mtime = os.stat(MASTER_PATH).st_mtime
    with _lock:
        if _cache is None or _cache[0] != mtime:
            _cache = (mtime, parse_master(MASTER_PATH.read_text(encoding='utf-8')))
        return _cache[1]


def bound(entity: dict[str, Any]) -> bool:
    return 'dataset' in entity


def dataset_ids(src: dict[str, Any]) -> list[int]:
    return sorted({src[n]['dataset'] for n in ENTITIES if bound(src[n])})


# ============================================================================ loading (grouped counts, server-side)
Executor = Callable[[int, list[dict[str, Any]]], list[list[dict[str, Any]]]]


def _query(columns: list[str], metrics: list[dict[str, Any]]) -> dict[str, Any]:
    return {'columns': columns, 'metrics': metrics, 'filters': [], 'time_range': 'No filter', 'row_limit': LOAD_LIMIT + 1,
            'orderby': [], 'extras': {'having': '', 'where': ''}}


def _sum(col: str, label: str) -> dict[str, Any]:
    return {'expressionType': 'SIMPLE', 'aggregate': 'SUM', 'column': {'column_name': col}, 'label': label}


def _vkey(row: dict[str, Any], cols: list[str]) -> str:
    return '|'.join('' if row.get(c) is None else str(row.get(c)) for c in cols)


def _cat(value: Any, cats: list[tuple[str, list[str]]]) -> str | None:
    if value is None:
        return None
    text = str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)
    return next((label for label, raw in cats if text in raw), None)


def load(src: dict[str, Any], master: dict[str, Any], execute: Executor) -> dict[str, Any]:
    """Grouped counts for every bound entity of a source (one Superset call per dataset, cached by the caller)."""
    def fetch(entity: dict[str, Any], columns: list[str], metrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cols = list(dict.fromkeys(c for c in columns if c))
        rows = execute(entity['dataset'], [_query(cols, metrics)])[0]
        if len(rows) > LOAD_LIMIT:
            raise RuntimeError(f"Dataset {entity['dataset']} has more than {LOAD_LIMIT} groups; too large for the Master template.")
        return rows

    V = src['villages']
    roles = {r: c for r, c in (V.get('roles') or {}).items() if isinstance(c, str)}
    level_cols = {lvl: c for lvl, c in V['levels'].items()}
    vcols = list(V['key']) + [c if isinstance(c, str) else c.get('site_map') or c['from'] for c in level_cols.values()] + list(roles.values())
    villages = []
    for row in fetch(V, vcols, [COUNT]):
        levels = {}
        for lvl, c in level_cols.items():
            if isinstance(c, str):
                levels[lvl] = row.get(c)
            elif 'site_map' in c:
                levels[lvl] = master['countries'].get(str(row.get(c['site_map'])))
            else:  # a level derived from another column through a registered map (e.g. district -> HDSS site)
                levels[lvl] = c['map'].get(str(row.get(c['from'])))
            if levels[lvl] in ('', None):  # a blank value is "not recorded", never a filter option
                levels[lvl] = None
        num = lambda r: (float(row[roles[r]]) if roles.get(r) and row.get(roles[r]) is not None else None)
        villages.append({'key': _vkey(row, V['key']), 'levels': levels,
                         'tier': row.get(roles['tier']) if roles.get('tier') else None,
                         'lat': num('latitude'), 'lon': num('longitude'), 'vuln': num('vulnerability'), 'cases': num('cases'),
                         'pop': num('population'), 'ever': num('ever_registered'), 'u5e': num('under_five_ever'),
                         'opd': num('outpatient_visits')})
    villages.sort(key=lambda v: v['key'])

    P = src['persons']
    pdims = {n: d for n, d in (P.get('dimensions') or {}).items() if '_cats' in d}
    rescol = P['resident']['column'] if P.get('resident') else None
    persons = []
    for row in fetch(P, list(P['village']) + [P['household']] + [d['column'] for d in pdims.values()] + ([rescol] if rescol else []), [COUNT]):
        persons.append({'v': _vkey(row, P['village']), 'hh': str(row.get(P['household'])), 'n': int(row.get('n') or 0),
                        'age': _cat(row.get(pdims['age_band']['column']), pdims['age_band']['_cats']) if 'age_band' in pdims else None,
                        'sex': _cat(row.get(pdims['sex']['column']), pdims['sex']['_cats']) if 'sex' in pdims else None,
                        'resident': (str(row.get(rescol)) == str(P['resident']['value'])) if rescol else True})

    H = src['households']
    households: dict[str, dict[str, Any]] = {}
    if bound(H):
        hdims = {n: d for n, d in (H.get('dimensions') or {}).items() if '_cats' in d}
        if H.get('derived_from_persons'):
            for p in persons:
                if p['resident']:
                    households.setdefault(p['hh'], {'v': p['v'], 'housing': None, 'water': None, 'bednet': None})
        else:
            for row in fetch(H, [H['key']] + list(H['village']) + [d['column'] for d in hdims.values()], [COUNT]):
                households[str(row.get(H['key']))] = {'v': _vkey(row, H['village']),
                    **{n: (_cat(row.get(hdims[n]['column']), hdims[n]['_cats']) if n in hdims else None) for n in ('housing', 'water', 'bednet')}}

    E = src['events']
    events = []
    if bound(E):
        for row in fetch(E, list(E['village']) + [E['types']['column'], E['date']], [COUNT]):
            t = _cat(row.get(E['types']['column']), E['types']['_cats'])
            d = _day(row.get(E['date']))
            if t:
                events.append({'v': _vkey(row, E['village']), 't': t, 'date': d, 'n': int(row.get('n') or 0)})

    sites: list[dict[str, Any]] = []
    S = src['sites']
    if bound(S):
        r = S['roles']
        for row in fetch(S, [S['key']] + list(S['village']) + [r['name'], S['status']['column'], r['reports'], r['rrt'], r['lag']], [COUNT]):
            sites.append({'key': str(row.get(S['key'])), 'name': str(row.get(r['name'])), 'v': _vkey(row, S['village']),
                          'st': _cat(row.get(S['status']['column']), S['status']['_cats']), 'rep': float(row.get(r['reports']) or 0),
                          'rrt': float(row.get(r['rrt']) or 0), 'lag': float(row.get(r['lag']) or 0), 'syn': {}, 'lab': {}})
        sites.sort(key=lambda s: int(s['key']) if s['key'].isdigit() else s['key'])
    by_site = {s['key']: s for s in sites}
    week_labels: dict[date, str] = {}
    W = src['site_weeks']
    if bound(W) and sites:
        for row in fetch(W, [W['site'], W['date'], W['week_label'], W['syndromes']['column']], [_sum(W['count'], 'v')]):
            s = by_site.get(str(row.get(W['site'])))
            syn = _cat(row.get(W['syndromes']['column']), W['syndromes']['_cats'])
            d = _day(row.get(W['date']))
            if s and syn and d:
                s['syn'].setdefault(syn, {})
                s['syn'][syn][d] = s['syn'][syn].get(d, 0) + float(row.get('v') or 0)
                week_labels[d] = str(row.get(W['week_label']))
    L = src['site_lab']
    if bound(L) and sites:
        for row in fetch(L, [L['site'], L['pathogens']['column']], [_sum(L['count'], 'v')]):
            s = by_site.get(str(row.get(L['site'])))
            pth = _cat(row.get(L['pathogens']['column']), L['pathogens']['_cats'])
            if s and pth:
                s['lab'][pth] = s['lab'].get(pth, 0) + float(row.get('v') or 0)
    signals = []
    G = src['signals']
    if bound(G) and sites:
        r = G['roles']
        rows = fetch(G, [G['key'], G['site'], r['label'], r['syndrome'], G['severity']['column'], r['detected']], [COUNT])
        order = lambda x: (0, int(x[G['key']])) if str(x.get(G['key'])).isdigit() else (1, str(x.get(G['key'])))
        for row in sorted(rows, key=order):  # table order = the registered signal order
            signals.append({'site': str(row.get(G['site'])), 'label': str(row.get(r['label'])),
                            'syn': row.get(r['syndrome']) if row.get(r['syndrome']) else None,
                            'sev': _cat(row.get(G['severity']['column']), G['severity']['_cats']), 'when': str(row.get(r['detected']) or '')})
    clusters = []
    C = src['clusters']
    if bound(C):
        r = C['roles']
        for row in fetch(C, list(C['village']) + [r['cases'], r['vulnerability'], r['radius'], r['p_value']], [COUNT]):
            clusters.append({'v': _vkey(row, C['village']), 'cases': float(row.get(r['cases']) or 0), 'vuln': float(row.get(r['vulnerability']) or 0),
                             'rad': float(row.get(r['radius']) or 0), 'p': float(row.get(r['p_value']) or 0)})
    dates = [e['date'] for e in events if e['date']] + list(week_labels)
    return {'villages': villages, 'persons': persons, 'households': households, 'events': events, 'sites': sites,
            'signals': signals, 'clusters': clusters, 'week_labels': {d.isoformat(): l for d, l in week_labels.items()},
            'date_min': min(dates).isoformat() if dates else None}


# ============================================================================ filter state
MON = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')


def allowed_values(src: dict[str, Any], master: dict[str, Any]) -> dict[str, list[str]]:
    """Every value the browser may send for each filter (labels come from the registry)."""
    def cats(entity: str, attr: str | None, sub: str | None = None) -> list[str] | None:
        e = src[entity]
        if not bound(e):
            return None
        d = (e.get('dimensions') or {}).get(sub) if sub else e.get(attr)
        return [label for label, _ in d['_cats']] if d and '_cats' in d else None
    V = src['villages']
    tier_bound = V.get('tiers') and isinstance((V.get('roles') or {}).get('tier'), str)
    values = {
        'tier': list(V['tiers']) if tier_bound else None,
        'event_type': cats('events', 'types'), 'age_band': cats('persons', None, 'age_band'), 'sex': cats('persons', None, 'sex'),
        'housing': cats('households', None, 'housing'), 'water': cats('households', None, 'water'),
        'bednet': cats('households', None, 'bednet'), 'syndrome': cats('site_weeks', 'syndromes'),
        'pathogen': cats('site_lab', 'pathogens'), 'site_status': cats('sites', 'status'),
        'signal_severity': cats('signals', 'severity')}
    return {k: v for k, v in values.items() if v}


def parse_state(raw: Any, src: dict[str, Any], master: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """Check a browser filter state: every key a known filter, every value a registered option."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise StateError('The filter state must be an object.')
    unknown = set(raw) - {'geo', 'cycle', 'period', 'time_point', 'dims'}
    if unknown:
        raise StateError(f'Unknown filter keys: {sorted(unknown)}.')
    state: dict[str, Any] = {'geo': {}, 'cycle': 'All', 'period': 'All', 'time_point': '', 'dims': {}}
    geo = raw.get('geo') or {}
    if not isinstance(geo, dict):
        raise StateError('geo must be an object.')
    for level, value in geo.items():
        if level not in src['geography'] + src['drill']:
            raise StateError(f'{level!r} is not a geography filter on this dashboard.')
        if value in (None, '', 'All'):
            continue
        if not isinstance(value, str) or value not in {v['levels'].get(level) for v in data['villages']}:
            raise StateError(f'{level} {value!r} is not in this dataset.')
        state['geo'][level] = value
    cycle = raw.get('cycle') or 'All'
    if cycle != 'All' and cycle not in [c['label'] for c in master['cycles']]:
        raise StateError(f'Update cycle {cycle!r} is not available.')
    period = raw.get('period') or 'All'
    if period not in RANGES:
        raise StateError(f'Time period {period!r} is not available.')
    state['cycle'], state['period'] = cycle, period
    tp = raw.get('time_point') or ''
    if tp and (not isinstance(tp, str) or len(tp) not in (4, 7) or not tp[:4].isdigit()):
        raise StateError('time_point must be a YYYY or YYYY-MM bucket.')
    state['time_point'] = tp
    allowed = allowed_values(src, master)
    dims = raw.get('dims') or {}
    if not isinstance(dims, dict):
        raise StateError('dims must be an object.')
    for name, value in dims.items():
        if value in (None, ''):
            continue
        if name not in allowed:
            raise StateError(f'{name!r} is not a filter on this dashboard.')
        if not isinstance(value, str) or value not in allowed[name]:
            raise StateError(f'{value!r} is not a {name} value.')
        state['dims'][name] = value
    return state


def reference(src: dict[str, Any]) -> date:
    return date.fromisoformat(src['reference_date']) if src.get('reference_date') else datetime.now(timezone.utc).date()


def time_points(src: dict[str, Any], master: dict[str, Any], data: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """The displayed timeline: Update cycle AND Time period, from the first record to the reference date, in
    calendar months (years when the span is more than three years)."""
    ref = reference(src)
    lo, hi = range_bounds(state['period'], ref)
    if state['cycle'] != 'All':
        c = next(c for c in master['cycles'] if c['label'] == state['cycle'])
        lo = max([x for x in (lo, c['from']) if x])
        hi = min([x for x in (hi, c['to']) if x])
    dmin = date.fromisoformat(data['date_min']) if data.get('date_min') else ref
    first = max([x for x in (lo, dmin) if x])
    last = min([x for x in (hi, ref) if x])
    window = (lo, hi)
    if first > last:
        return {'points': [], 'window': window, 'unit': 'month'}
    months = (last.year - first.year) * 12 + last.month - first.month + 1
    unit = 'month' if months <= 36 else 'year'
    points, d = [], date(first.year, first.month if unit == 'month' else 1, 1)
    while d <= last:
        nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1) if unit == 'month' else date(d.year + 1, 1, 1)
        points.append({'key': d.strftime('%Y-%m') if unit == 'month' else str(d.year), 'start': d, 'end': nxt - timedelta(days=1)})
        d = nxt
    single_year = len({p['start'].year for p in points}) == 1
    for p in points:
        if unit == 'month':
            p['label'] = MON[p['start'].month - 1] if single_year else f"{MON[p['start'].month - 1]} {p['start'].year}"
        else:
            p['label'] = p['key']
        week = next((lab for k, lab in sorted(data['week_labels'].items()) if p['start'] <= date.fromisoformat(k) <= p['end']), None)
        if week:
            p['week'] = week
    return {'points': points, 'window': window, 'unit': unit}


# ============================================================================ the design's filter model, on the server
def compute(src: dict[str, Any], master: dict[str, Any], data: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    S, D = state, state['dims']
    k_min = src['min_cell_size']
    flags = {'suppressed': False}

    def cell(n: float | None) -> int | None:
        if n is None:
            return None
        n = int(round(n))
        if 0 < n < k_min:
            flags['suppressed'] = True
            return None
        return n

    VILL, HH, EVT = data['villages'], data['households'], data['events']
    SITES, SIGS, CLUS = data['sites'], data['signals'], data['clusters']
    PER = [p for p in data['persons'] if p['resident']]
    by_v = {v['key']: v for v in VILL}
    tl = time_points(src, master, data, S)
    points = tl['points']
    base = [p['key'] for p in points]
    w_lo, w_hi = tl['window']
    unit = tl['unit']

    def pkey(d: date) -> str:
        return d.strftime('%Y-%m') if unit == 'month' else str(d.year)

    def in_window(d: date | None) -> bool:
        return d is not None and (w_lo is None or d >= w_lo) and (w_hi is None or d <= w_hi)

    levels = src['geography'] + src['drill']
    is_on = {'hous': 'housing' in D, 'wat': 'water' in D, 'net': 'bednet' in D, 'age': 'age_band' in D, 'sex': 'sex' in D,
             'evT': 'event_type' in D, 'syn': 'syndrome' in D, 'path': 'pathogen' in D, 'status': 'site_status' in D,
             'sev': 'signal_severity' in D, 'tier': 'tier' in D, 'tIdx': bool(S['time_point'])}

    def on(k: str, skip: set[str]) -> bool:
        return is_on[k] and k not in skip

    def per_of(skip: set[str]) -> list[str]:
        return [S['time_point']] if on('tIdx', skip) and S['time_point'] in base else base

    def geo_ok(v: dict[str, Any]) -> bool:
        return all(v['levels'].get(level) == value for level, value in S['geo'].items())

    def hh_ok(h: dict[str, Any], skip: set[str]) -> bool:
        return ((not on('hous', skip) or h['housing'] == D.get('housing')) and (not on('wat', skip) or h['water'] == D.get('water'))
                and (not on('net', skip) or h['bednet'] == D.get('bednet')))

    def per_ok(p: dict[str, Any], skip: set[str]) -> bool:
        return (not on('age', skip) or p['age'] == D.get('age_band')) and (not on('sex', skip) or p['sex'] == D.get('sex'))

    empty_hh = {'v': None, 'housing': None, 'water': None, 'bednet': None}

    def persons_in(ids: set[str], skip: set[str]) -> list[dict[str, Any]]:
        return [p for p in PER if p['v'] in ids and per_ok(p, skip) and hh_ok(HH.get(p['hh'], empty_hh), skip)]

    def households_in(ids: set[str], skip: set[str]) -> list[str]:
        ok_h = {p['hh'] for p in persons_in(ids, skip)} if on('age', skip) or on('sex', skip) else None
        return [k for k, h in HH.items() if h['v'] in ids and hh_ok(h, skip) and (ok_h is None or k in ok_h)]

    def ev_in(e: dict[str, Any], per: list[str]) -> bool:
        return e['date'] is not None and pkey(e['date']) in per and in_window(e['date'])

    def syn_total(s: dict[str, Any], name: str | None, per: list[str]) -> float:
        return sum(v for d, v in s['syn'].get(name, {}).items() if pkey(d) in per and in_window(d))

    def site_ok(s: dict[str, Any], skip: set[str], per: list[str]) -> bool:
        return ((not on('status', skip) or s['st'] == D.get('site_status'))
                and (not on('syn', skip) or syn_total(s, D.get('syndrome'), per) > 0)
                and (not on('path', skip) or s['lab'].get(D.get('pathogen'), 0) > 0)
                and (not on('sev', skip) or any(g['site'] == s['key'] and g['sev'] == D.get('signal_severity') for g in SIGS)))

    def scope(skip: set[str]) -> tuple[set[str], list[str]]:
        per = per_of(skip)
        ids = {v['key'] for v in VILL if geo_ok(v) and (not on('tier', skip) or v['tier'] == D.get('tier'))}
        if any(on(k, skip) for k in ('hous', 'wat', 'net', 'age', 'sex')):
            ids &= {HH[k]['v'] for k in households_in(ids, skip)}
        if on('evT', skip):
            ids &= {e['v'] for e in EVT if e['t'] == D.get('event_type') and ev_in(e, per)}
        if any(on(k, skip) for k in ('status', 'syn', 'path', 'sev')):
            ids &= {s['v'] for s in SITES if site_ok(s, skip, per)}
        return ids, per

    def sites_in(sc: tuple[set[str], list[str]], skip: set[str]) -> list[dict[str, Any]]:
        ids, per = sc
        return [s for s in SITES if s['v'] in ids and site_ok(s, skip, per)]

    def ev_count(sc: tuple[set[str], list[str]], t: str, per: list[str]) -> int:
        return sum(e['n'] for e in EVT if e['v'] in sc[0] and e['t'] == t and ev_in(e, per))

    def vpath(v: dict[str, Any]) -> dict[str, Any]:
        return {level: v['levels'].get(level) for level in levels}

    A = scope(set())
    ref = reference(src)
    dmin = date.fromisoformat(data['date_min']) if data.get('date_min') else ref
    out: dict[str, Any] = {
        'empty': not A[0], 'unit': unit,
        'points': [{k: p[k] for k in ('key', 'label', 'week') if k in p} for p in points],
        'paths': [vpath(v) for v in VILL],
        'cycles': [c['label'] for c in master['cycles'] if c['to'] >= dmin and c['from'] <= ref],
    }

    # ---------------------------------------------------------------- overview
    types = [label for label, _ in src['events']['types']['_cats']] if bound(src['events']) else []
    pe, hs_a = persons_in(A[0], set()), households_in(A[0], set())
    VA = [v for v in VILL if v['key'] in A[0]]
    ov: dict[str, Any] = {'residents': cell(sum(p['n'] for p in pe)), 'households': cell(len(hs_a)),
                          'under_five': cell(sum(p['n'] for p in pe if p['age'] == '0-4'))}
    if types:
        b, d_, i_, o_ = (ev_count(A, t, A[1]) if t in types else None for t in ('Births', 'Deaths', 'In-migration', 'Out-migration'))
        ov['births'], ov['deaths'] = cell(b), cell(d_)
        ov['net_migration'] = None if cell(i_) is None or cell(o_) is None else i_ - o_
        if 'Pregnancies' in types:  # the design counts pregnancy events in scope over the whole timeline
            ov['currently_pregnant'] = cell(sum(e['n'] for e in EVT if e['v'] in A[0] and e['t'] == 'Pregnancies'))

    def vsum(attr: str) -> float | None:
        return None if all(v[attr] is None for v in VILL) else sum(v[attr] or 0 for v in VA)

    ever = vsum('ever')
    if ever is None and src['persons'].get('resident'):  # real data: every person record in scope, residents or not
        ever = sum(p['n'] for p in data['persons'] if p['v'] in A[0])
    ov['ever_registered'], ov['under_five_ever'], ov['outpatient_visits'] = cell(ever), cell(vsum('u5e')), cell(vsum('opd'))
    if types:
        C1 = scope({'evT', 'tIdx'})
        ov['vital'] = {'points': C1[1], 'series': {t: [cell(sum(e['n'] for e in EVT if e['v'] in C1[0] and e['t'] == t and e['date'] is not None
                                                             and pkey(e['date']) == m and in_window(e['date']))) for m in C1[1]] for t in types}}
        C2 = scope({'evT'})
        ov['mix'] = {t: cell(ev_count(C2, t, C2[1])) for t in types}
    pdims = src['persons'].get('dimensions') or {}
    if '_cats' in pdims.get('age_band', {}) and '_cats' in pdims.get('sex', {}):
        C3 = scope({'age', 'sex'})
        p3 = persons_in(C3[0], {'age', 'sex'})
        bands = [label for label, _ in pdims['age_band']['_cats']]
        sexes = [label for label, _ in pdims['sex']['_cats']]
        ov['pyramid'] = {'bands': bands, 'sexes': sexes,
                         'counts': {s: [cell(sum(p['n'] for p in p3 if p['age'] == a and p['sex'] == s)) for a in bands] for s in sexes}}
    hdims = (src['households'].get('dimensions') or {}) if bound(src['households']) else {}
    for dim, short in (('housing', 'hous'), ('water', 'wat'), ('bednet', 'net')):
        if '_cats' in hdims.get(dim, {}):
            C = scope({short})
            hs = households_in(C[0], {short})
            ov[dim] = {label: cell(sum(1 for k in hs if HH[k][dim] == label)) for label, _ in hdims[dim]['_cats']}
    if VILL and all(v['lat'] is not None for v in VILL):
        pv: dict[str, int] = {}
        for p in pe:
            pv[p['v']] = pv.get(p['v'], 0) + p['n']
        ov['pins'] = [{'path': vpath(v), 'village': v['levels'].get('village'), 'sub': v['levels'].get('subcounty'),
                       'lat': v['lat'], 'lon': v['lon'], 'residents': cell(pv[v['key']])}
                      for v in VILL if v['key'] in pv and cell(pv[v['key']]) is not None]
    out['overview'] = ov

    # ---------------------------------------------------------------- sentinel
    if SITES:
        SS = sites_in(A, set())
        ss_keys = {s['key'] for s in SS}
        sig_f = [g for g in SIGS if g['site'] in ss_keys and ('signal_severity' not in D or g['sev'] == D['signal_severity'])
                 and ('syndrome' not in D or g['syn'] == D['syndrome'])]
        rep = sum(s['rep'] for s in SS)
        syns = [label for label, _ in src['site_weeks']['syndromes']['_cats']] if bound(src['site_weeks']) else []
        pathogens = [label for label, _ in src['site_lab']['pathogens']['_cats']] if bound(src['site_lab']) else []
        sen: dict[str, Any] = {'sites': len(SS), 'reports': rep, 'signals': len(sig_f), 'rrt': sum(s['rrt'] for s in SS),
                               'lab': sum(sum(s['lab'].values()) for s in SS),
                               'lag': float(format(sum(s['lag'] * s['rep'] for s in SS) / rep, '.1f')) if rep else None}
        if syns:
            C5 = scope({'syn', 'tIdx'})
            SS5 = sites_in(C5, {'syn', 'tIdx'})
            sen['weekly'] = {'points': C5[1], 'series': {n: [sum(v for s in SS5 for d, v in s['syn'].get(n, {}).items()
                                                                 if pkey(d) == w and in_window(d)) for w in C5[1]] for n in syns}}
        C6 = scope({'status'})
        SS6 = sites_in(C6, {'status'})
        sen['site_pins'] = [{'name': s['name'], 'status': s['st'], 'path': vpath(by_v[s['v']]), 'village': by_v[s['v']]['levels'].get('village'),
                             'lat': by_v[s['v']]['lat'], 'lon': by_v[s['v']]['lon']} for s in SS6 if s['v'] in by_v]
        sen['status_counts'] = {st: sum(1 for s in SS6 if s['st'] == st) for st, _ in src['sites']['status']['_cats']}
        if pathogens:
            C7 = scope({'path'})
            SS7 = sites_in(C7, {'path'})
            sen['pathogen'] = {p: sum(s['lab'].get(p, 0) for s in SS7) for p in pathogens}
        site_by_key = {s['key']: s for s in SITES}
        sen['signal_rows'] = [{'facility': site_by_key[g['site']]['name'], 'path': vpath(by_v[site_by_key[g['site']]['v']]),
                               'label': g['label'], 'syndrome': g['syn'], 'severity': g['sev'], 'when': g['when']} for g in sig_f]
        out['sentinel'] = sen

    # ---------------------------------------------------------------- geospatial
    if any(v['tier'] for v in VILL):
        C8 = scope({'tier'})
        tiers = list(src['villages']['tiers'])
        tc = {t: sum(1 for v in VILL if v['key'] in C8[0] and v['tier'] == t) for t in tiers}
        cl_f = sorted((c for c in CLUS if c['v'] in A[0]), key=lambda c: -c['cases'])
        high = [v for v in VA if v['tier'] == tiers[0]]
        pop = sum(v['pop'] or 0 for v in VA)
        top = cl_f[0] if cl_f else None
        out['geo'] = {
            'tier_counts': tc, 'clusters': len(cl_f), 'high_risk': len(high), 'pop_at_risk': sum(v['pop'] or 0 for v in high),
            'mean_vulnerability': math.floor(sum((v['pop'] or 0) * (v['vuln'] or 0) for v in VA) / pop + 0.5) if pop else None,  # JS Math.round
            'radius': top['rad'] if top else None, 'p_value': top['p'] if top else None,
            'pins': [{'path': vpath(v), 'village': v['levels'].get('village'), 'lat': v['lat'], 'lon': v['lon'], 'cases': v['cases'] or 0,
                      'tier': v['tier']} for v in VA],
            'scatter': [{'path': vpath(by_v[c['v']]), 'village': by_v[c['v']]['levels'].get('village'), 'cases': c['cases'],
                         'vulnerability': c['vuln']} for c in cl_f],
            'hotspots': [{'path': vpath(by_v[c['v']]), 'village': by_v[c['v']]['levels'].get('village'), 'cases': c['cases'],
                          'tier': by_v[c['v']]['tier'], 'vulnerability': c['vuln']} for c in cl_f[:6]],
        }
    out['suppression'] = {'min_cell_size': k_min, 'applied': flags['suppressed']}
    return out


# ============================================================================ spec (what Vima emits) and coverage
TABS = (('overview', 'Overview'), ('sentinel', 'LifeHealth Sentinel'), ('geo', 'Geospatial Cluster'))
TAB_SLOTS = {
    'overview': ('kpi.residents', 'kpi.households', 'kpi.under_five', 'kpi.births', 'kpi.deaths', 'kpi.net_migration',
                 'chart.vital_events_by_month', 'chart.event_mix', 'map.events', 'stat.ever_registered', 'stat.currently_pregnant',
                 'stat.under_five_ever', 'stat.outpatient_visits', 'chart.age_gender', 'chart.housing', 'chart.water_sanitation', 'chart.bednet'),
    'sentinel': ('kpi.sites', 'kpi.reports', 'kpi.signals', 'kpi.rrt', 'kpi.lab', 'kpi.lag', 'chart.syndrome_weekly', 'map.sites',
                 'chart.pathogen', 'table.signals'),
    'geo': ('kpi.clusters', 'kpi.high_risk', 'kpi.pop_at_risk', 'kpi.mean_vulnerability', 'kpi.radius', 'kpi.p_value', 'map.clusters',
            'chart.tier_distribution', 'chart.cluster_scatter', 'table.hotspots'),
}
EXTRA_FILTERS = ('tier', 'cycle', 'period', 'time_point', 'event_type', 'age_band', 'sex', 'housing', 'water', 'bednet', 'syndrome',
                 'pathogen', 'site_status', 'signal_severity')


class MasterSpecError(SpecError):
    """The Master spec cannot be saved; the message says exactly why."""


def _reason(entity: dict[str, Any] | None, what: str) -> dict[str, str]:
    if entity and entity.get('status') in ('needs_definition', 'no_source'):
        return {'status': entity['status'], 'reason': entity['reason']}
    return {'status': 'no_source', 'reason': f'No registered source for {what}.'}


def coverage(src: dict[str, Any]) -> dict[str, Any]:
    """tab -> slot -> 'bound' | {status, reason}; filters likewise. The page hides unbound slots and empty tabs."""
    V, P, H, E = src['villages'], src['persons'], src['households'], src['events']
    vroles = V.get('roles') or {}
    role = lambda r: isinstance(vroles.get(r), str)
    role_why = lambda r: (_unbound(vroles[r]) if r in vroles and _unbound(vroles[r]) else {'status': 'no_source', 'reason': f'No {r} registered.'})
    types = [label for label, _ in E['types']['_cats']] if bound(E) else []
    unbound_types = (E.get('types') or {}).get('unbound') or {} if bound(E) else {}
    pdims = P.get('dimensions') or {}
    hdims = (H.get('dimensions') or {}) if bound(H) else {}

    def need(ok: bool, why: dict[str, str]) -> Any:
        return 'bound' if ok else why

    def event(t: str) -> Any:
        if t in types:
            return 'bound'
        if t in unbound_types:
            return {'status': 'needs_definition', 'reason': unbound_types[t]}
        return _reason(E if not bound(E) else None, f'{t} events')

    def hdim(name: str) -> Any:
        d = hdims.get(name)
        if d and '_cats' in d:
            return 'bound'
        return _unbound(d) if d and _unbound(d) else _reason(H if not bound(H) else None, name)

    sites_ok, sig_ok = bound(src['sites']), bound(src['sites']) and bound(src['signals'])
    cl_ok = bound(src['clusters'])
    slots = {
        'overview': {
            'kpi.residents': 'bound', 'kpi.households': need(bound(H), _reason(H, 'households')),
            'kpi.under_five': need('_cats' in pdims.get('age_band', {}) and any(l == '0-4' for l, _ in pdims['age_band'].get('_cats', [])),
                                   {'status': 'no_source', 'reason': 'No 0-4 age band registered.'}),
            'kpi.births': event('Births'), 'kpi.deaths': event('Deaths'),
            'kpi.net_migration': 'bound' if {'In-migration', 'Out-migration'} <= set(types) else event('In-migration'),
            'chart.vital_events_by_month': need(bound(E), _reason(E, 'events')), 'chart.event_mix': need(bound(E), _reason(E, 'events')),
            'map.events': 'bound' if role('latitude') and role('longitude') else role_why('latitude'),
            'stat.ever_registered': 'bound' if role('ever_registered') or P.get('resident') else role_why('ever_registered'),
            'stat.currently_pregnant': event('Pregnancies'),
            'stat.under_five_ever': 'bound' if role('under_five_ever') else role_why('under_five_ever'),
            'stat.outpatient_visits': 'bound' if role('outpatient_visits') else role_why('outpatient_visits'),
            'chart.age_gender': need('_cats' in pdims.get('age_band', {}) and '_cats' in pdims.get('sex', {}),
                                     {'status': 'no_source', 'reason': 'Age band and sex are not registered.'}),
            'chart.housing': hdim('housing'), 'chart.water_sanitation': hdim('water'), 'chart.bednet': hdim('bednet'),
        },
        'sentinel': {s: ('bound' if sites_ok else _reason(src['sites'], 'sentinel sites')) for s in TAB_SLOTS['sentinel']},
        'geo': {s: ('bound' if cl_ok else _reason(src['clusters'], 'clusters')) for s in TAB_SLOTS['geo']},
    }
    for s in ('kpi.signals', 'table.signals'):
        if sites_ok and not sig_ok:
            slots['sentinel'][s] = _reason(src['signals'], 'signals')
    if sites_ok and not bound(src['site_weeks']):
        slots['sentinel']['chart.syndrome_weekly'] = _reason(src['site_weeks'], 'weekly syndromes')
    if sites_ok and not bound(src['site_lab']):
        slots['sentinel']['chart.pathogen'] = slots['sentinel']['kpi.lab'] = _reason(src['site_lab'], 'laboratory results')
    tier_ok = role('tier') and bool(V.get('tiers'))
    slots['geo']['kpi.high_risk'] = slots['geo']['chart.tier_distribution'] = 'bound' if tier_ok else role_why('tier')
    slots['geo']['kpi.pop_at_risk'] = 'bound' if tier_ok and role('population') else role_why('population' if tier_ok else 'tier')
    slots['geo']['kpi.mean_vulnerability'] = 'bound' if role('vulnerability') and role('population') else role_why('vulnerability')
    slots['geo']['map.clusters'] = 'bound' if tier_ok and role('latitude') else role_why('latitude')
    allowed = allowed_values(src, {})
    timed = bound(E) or bound(src['site_weeks'])
    filters = {g: 'bound' for g in src['geography']}
    for f in EXTRA_FILTERS:
        if f in ('cycle', 'period', 'time_point'):
            filters[f] = 'bound' if timed else {'status': 'no_source', 'reason': 'No dated records registered.'}
        else:
            filters[f] = 'bound' if f in allowed else {'status': 'no_source', 'reason': f'No {f} values registered.'}
    tabs = {t: any(v == 'bound' for v in slots[t].values()) for t, _ in TABS}
    return {'slots': slots, 'filters': filters, 'tabs': tabs}


def _public(entity: dict[str, Any]) -> Any:
    """The registered binding as stored in a spec (internal parse keys removed)."""
    if isinstance(entity, dict):
        return {k: _public(v) for k, v in entity.items() if not k.startswith('_')}
    if isinstance(entity, list):
        return [_public(v) for v in entity]
    return entity


def build_master_spec(source_key: str, title: str) -> dict[str, Any]:
    reg = load_master()
    src = reg['sources'].get(source_key)
    if src is None:
        raise MasterSpecError(f'{source_key!r} is not a registered Master source. Registered: {sorted(reg["sources"])}.')
    cov = coverage(src)
    return {
        'template': TEMPLATE, 'version': 1, 'title': title, 'status': 'draft',
        'source': {'key': source_key, 'datasets': dataset_ids(src)},
        'entities': {name: _public(src[name]) for name in ENTITIES},
        'tabs': [{'id': t, 'label': label, 'slots': [s for s in TAB_SLOTS[t] if cov['slots'][t][s] == 'bound']} for t, label in TABS],
        'filters': {'geography': list(src['geography']), 'extra': [f for f in EXTRA_FILTERS if cov['filters'][f] == 'bound']},
        'privacy': {'person_level_rows_to_browser': False, 'min_cell_size': src['min_cell_size'], 'synthetic': src['synthetic']},
    }


def validate_master_spec(spec: Any, client=None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (source, coverage) or raise MasterSpecError with the first problem."""
    from lifedata_ai.hdss.spec import PERSONAL_NAME, MAX_TITLE
    if not isinstance(spec, dict):
        raise MasterSpecError('The spec must be an object.')
    allowed_keys = {'template', 'version', 'title', 'status', 'source', 'entities', 'tabs', 'filters', 'privacy'}
    extra = set(spec) - allowed_keys
    if extra:
        raise MasterSpecError(f'Unknown spec keys {sorted(extra)} (layout and HTML are not part of a spec).')
    if spec.get('template') != TEMPLATE or spec.get('version') != 1:
        raise MasterSpecError(f'template must be {TEMPLATE!r}, version 1.')
    title = spec.get('title')
    if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE:
        raise MasterSpecError(f'title must be 1-{MAX_TITLE} characters of text.')
    if spec.get('status') not in ('draft', 'published'):
        raise MasterSpecError('status must be draft or published.')
    reg = load_master()
    key = (spec.get('source') or {}).get('key')
    src = reg['sources'].get(key)
    if src is None:
        raise MasterSpecError(f'Source {key!r} is not registered for the Master template. Registered: {sorted(reg["sources"])}.')
    if spec['source'].get('datasets') != dataset_ids(src):
        raise MasterSpecError(f'Source {key} is registered with datasets {dataset_ids(src)}.')
    for name in ENTITIES:
        if (spec.get('entities') or {}).get(name) != _public(src[name]):
            raise MasterSpecError(f'Entity {name} must use the registered binding exactly; change master_bindings.yaml instead.')
        e = src[name]
        for column, role in ((e.get('columns') or {}).items() if bound(e) else ()):
            if PERSONAL_NAME.search(column):
                raise MasterSpecError(f'Column {column!r} looks like a personal or free-text field and cannot be used.')
    cov = coverage(src)
    tabs = spec.get('tabs')
    if not isinstance(tabs, list) or [t.get('id') for t in tabs] != [t for t, _ in TABS]:
        raise MasterSpecError('tabs must be overview, sentinel, geo in that order.')
    for tab in tabs:
        for slot in tab.get('slots') or []:
            if slot not in TAB_SLOTS[tab['id']]:
                raise MasterSpecError(f'Unknown slot {slot!r} on tab {tab["id"]}.')
            if cov['slots'][tab['id']][slot] != 'bound':
                why = cov['slots'][tab['id']][slot]
                raise MasterSpecError(f'Slot {slot} has no registered source ({why["status"]}: {why["reason"]}).')
    filters = spec.get('filters') or {}
    if filters.get('geography') != list(src['geography']):
        raise MasterSpecError(f'Geography filters must be {list(src["geography"])} for this source.')
    for f in filters.get('extra') or []:
        if f not in EXTRA_FILTERS or cov['filters'].get(f) != 'bound':
            raise MasterSpecError(f'Filter {f!r} is not available for this source.')
    privacy = spec.get('privacy') or {}
    if privacy.get('person_level_rows_to_browser') is not False:
        raise MasterSpecError('privacy.person_level_rows_to_browser must be false.')
    if privacy.get('synthetic') != src['synthetic']:
        raise MasterSpecError(f'privacy.synthetic must be {src["synthetic"]} (registered).')
    if type(privacy.get('min_cell_size')) is not int or privacy['min_cell_size'] < src['min_cell_size']:
        raise MasterSpecError(f'privacy.min_cell_size cannot be below the registered {src["min_cell_size"]}.')
    if client is not None:
        for name in ENTITIES:
            e = src[name]
            if not bound(e):
                continue
            dataset = client.get_dataset(e['dataset'])
            if not dataset:
                raise MasterSpecError(f'Dataset {e["dataset"]} ({name}) does not exist or you cannot read it.')
            available = {c.get('column_name') for c in dataset.get('columns', [])}
            missing = sorted(set(e.get('columns') or {}) - available)
            if missing:
                raise MasterSpecError(f'Column(s) {missing} do not exist in Superset dataset {e["dataset"]} ({name}).')
    return src, cov


def page_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """What the browser receives: tabs and slots that bind, filter options, coverage reasons. No layout."""
    reg = load_master()
    src, cov = validate_master_spec(spec)
    allowed = allowed_values(src, reg)
    return {
        'template': TEMPLATE, 'title': spec['title'], 'status': spec['status'], 'privacy': spec['privacy'],
        'tabs': [dict(t, visible=cov['tabs'][t['id']]) for t in spec['tabs']],
        'geography': spec['filters']['geography'], 'drill': src['drill'], 'filters': spec['filters']['extra'],
        'options': {k: v for k, v in allowed.items() if k in spec['filters']['extra']},
        'tiers': list(src['villages'].get('tiers') or []), 'coverage': cov, 'reference_date': src.get('reference_date'),
    }


def unbound_summary(cov: dict[str, Any]) -> list[str]:
    lines = []
    for tab, slots in cov['slots'].items():
        if not cov['tabs'][tab]:
            first = next(iter(slots.values()))
            lines.append(f"tab {tab} hidden: {first['status']}: {first['reason']}")
            continue
        lines += [f"{tab} {slot}: {why['status']}: {why['reason']}" for slot, why in slots.items() if why != 'bound']
    lines += [f"filter {f}: {why['status']}: {why['reason']}" for f, why in cov['filters'].items() if why != 'bound']
    return lines
