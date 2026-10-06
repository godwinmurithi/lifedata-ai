"""Versioned publication design contract; Buikwe is the visual benchmark."""
import re

DESIGN_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'baseline': {'enum': ['vima-buikwe-v1']},
        'colors': {'type': 'object', 'additionalProperties': False,
                   'properties': {k: {'type': 'string', 'pattern': '^#[0-9a-fA-F]{6}$'}
                                  for k in ('accent', 'yes', 'no', 'unknown') }},
        'palette': {'type': 'array', 'minItems': 2, 'maxItems': 12,
                    'items': {'type': 'string', 'pattern': '^#[0-9a-fA-F]{6}$'}},
        'chart_styles': {'type': 'object', 'additionalProperties': {
            'type': 'object', 'additionalProperties': False, 'properties': {
                'color': {'type': 'string', 'pattern': '^#[0-9a-fA-F]{6}$'},
                'colors': {'type': 'object', 'additionalProperties': False, 'properties': {
                    key: {'type': 'string', 'pattern': '^#[0-9a-fA-F]{6}$'} for key in ('accent', 'yes', 'no', 'unknown')}},
                'palette': {'type': 'array', 'minItems': 2, 'maxItems': 12, 'items': {'type': 'string', 'pattern': '^#[0-9a-fA-F]{6}$'}}}}},
        'groups': {'type': 'array', 'maxItems': 30, 'items': {
            'type': 'object', 'additionalProperties': False,
            'properties': {'title': {'type': 'string', 'minLength': 1, 'maxLength': 80},
                           'chart_ids': {'type': 'array', 'minItems': 2, 'maxItems': 20,
                                         'items': {'type': 'integer', 'minimum': 1}}},
            'required': ['title', 'chart_ids']}},
    },
}

def validate_design(design, chart_ids):
    def invalid(message):
        from lifedata_ai.tools.base import ToolError
        raise ToolError(message, code='invalid_design')
    if not isinstance(design, dict) or set(design) - set(DESIGN_SCHEMA['properties']):
        invalid('Unsupported dashboard design settings.')
    if design.get('baseline', 'vima-buikwe-v1') != 'vima-buikwe-v1':
        invalid('Use the vima-buikwe-v1 design baseline.')
    colors = design.get('colors', {})
    if not isinstance(colors, dict) or set(colors) - {'accent', 'yes', 'no', 'unknown'}:
        invalid('Unsupported semantic color.')
    palette = design.get('palette', ['#13a58a', '#e15b6c'])
    if not isinstance(palette, list) or not 2 <= len(palette) <= 12:
        invalid('Provide between 2 and 12 palette colors.')
    if any(not isinstance(c, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', c)
           for c in [*colors.values(), *palette]):
        invalid('Colors must use six-digit hex notation.')
    styles = design.get('chart_styles', {})
    if not isinstance(styles, dict):
        invalid('Chart styles must be an object.')
    for target, style in styles.items():
        if target != 'gauge' and not (isinstance(target, str) and target.startswith('chart:') and target[6:].isdigit() and int(target[6:]) in chart_ids) and not (isinstance(target, str) and target.startswith('field:') and len(target) > 6):
            invalid('Style targets must be gauge, chart:<source ID>, or field:<current field>.')
        if not isinstance(style, dict) or set(style) - {'color', 'colors', 'palette'}:
            invalid('Unsupported individual chart style.')
        if 'color' in style and (not isinstance(style['color'], str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', style['color'])):
            invalid('Chart color must use six-digit hex notation.')
        validate_design({k: v for k, v in style.items() if k != 'color'}, chart_ids)
    groups = design.get('groups', [])
    if not isinstance(groups, list) or len(groups) > 30:
        invalid('Provide at most 30 chart groups.')
    seen = set()
    for group in groups:
        if not isinstance(group, dict) or set(group) != {'title', 'chart_ids'}:
            invalid('Each group needs a title and chart_ids.')
        title, ids = group['title'], group['chart_ids']
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 80:
            invalid('Group titles must contain 1 to 80 characters.')
        if not isinstance(ids, list) or not 2 <= len(ids) <= 20:
            invalid('Group between 2 and 20 related charts per card.')
        for identifier in ids:
            if type(identifier) is not int or identifier not in chart_ids or identifier in seen:
                invalid('Grouped charts must belong to this dashboard and occur in only one group.')
            seen.add(identifier)
    return {'baseline': 'vima-buikwe-v1', **design}
