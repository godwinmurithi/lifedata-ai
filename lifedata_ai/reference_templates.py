"""Execute a curated reference without rewriting its source or reusing its facts.

The registered file is immutable. A derivative document supplies its missing
runtime. Live mode does not execute the illustrative dataset or analytics script.
"""
import json
from hashlib import sha256
from html import escape
from pathlib import Path
import re

from lifedata_ai.composition import ENCODING_SCHEMA
from lifedata_ai.tools.base import ToolError
from lifedata_ai.administrative import ADMINISTRATIVE_SCHEMA, CATALOGUE_SCHEMA

MASTER = Path(__file__).parent / "dashboard_templates" / "sincep-hdss.html"
MASTER_SHA256 = "37f8fb2337afbaae241b3e7e3ca7e030606f3da48f6bbb709b9abbdb16cc0863"
# Stable semantic slots identify original panels; their coordinates/styles stay
# in the supplied document, never in a reconstructed imitation.
REFERENCE_SLOTS = {
    "residents": ("Residents", "overview", "kpi"),
    "households": ("Households", "overview", "kpi"),
    "under_five": ("Under five", "overview", "kpi"),
    "births": ("Births", "overview", "kpi"),
    "deaths": ("Deaths", "overview", "kpi"),
    "net_migration": ("Net migration", "overview", "kpi"),
    "vital_events": ("Vital events by month", "overview", "chart"),
    "event_mix": ("Event mix", "overview", "chart"),
    "event_map": ("Where events are happening", "overview", "chart"),
    "age_gender": ("Age and gender", "overview", "chart"),
    "housing": ("Housing quality", "overview", "chart"),
    "water": ("Water & sanitation", "overview", "chart"),
    "bednets": ("Bednet use", "overview", "chart"),
    "sentinel_sites": ("Active sentinel sites", "sentinel", "kpi"),
    "weekly_reports": ("Reports this week", "sentinel", "kpi"),
    "signals_detected": ("Signals detected", "sentinel", "kpi"),
    "rrt_deployments": ("RRT deployments", "sentinel", "kpi"),
    "lab_confirmations": ("Lab confirmations", "sentinel", "kpi"),
    "reporting_lag": ("Avg reporting lag", "sentinel", "kpi"),
    "syndrome_trends": ("Syndrome trends (weekly)", "sentinel", "chart"),
    "site_status": ("Sentinel site status", "sentinel", "chart"),
    "pathogens": ("Pathogen breakdown", "sentinel", "chart"),
    "surveillance_signals": ("Recent surveillance signals", "sentinel", "chart"),
    "clusters": ("Clusters detected", "geo", "kpi"),
    "high_risk": ("High-risk villages", "geo", "kpi"),
    "population_at_risk": ("Population at risk", "geo", "kpi"),
    "vulnerability": ("Mean vulnerability", "geo", "kpi"),
    "hotspot_radius": ("Hotspot radius", "geo", "kpi"),
    "scan_p": ("Scan p-value", "geo", "kpi"),
    "cluster_map": ("Detected clusters & vulnerability", "geo", "chart"),
    "vulnerability_tiers": ("Vulnerability tier distribution", "geo", "chart"),
    "cluster_scatter": ("Cluster size vs vulnerability", "geo", "chart"),
    "hotspot_villages": ("Top hotspot villages", "geo", "chart"),
}
GEOGRAPHY_BINDING_SCHEMA = {'type': 'object', 'additionalProperties': False, 'properties': {
    'source_chart_id': {'type': 'integer', 'minimum': 1},
    'site_field': {'type': 'string'},
    'administrative_boundaries': ADMINISTRATIVE_SCHEMA,
    'boundary_catalogue': CATALOGUE_SCHEMA,
}, 'required': ['source_chart_id']}
BINDING_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "chart_ids": {"type": "array", "minItems": 1, "maxItems": 20, "uniqueItems": True, "items": {"type": "integer", "minimum": 1}},
    "title": {"type": "string", "minLength": 1, "maxLength": 100},
    "encoding": ENCODING_SCHEMA,
    "geography": GEOGRAPHY_BINDING_SCHEMA,
}, "required": ["chart_ids"]}
REFERENCE_TEMPLATE_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "template": {"const": "sincep-hdss"},
    "bindings": {"type": "object", "minProperties": 1, "properties": {key: BINDING_SCHEMA for key in REFERENCE_SLOTS}, "additionalProperties": False},
}, "required": ["template", "bindings"]}


def validate_reference_template(config, chart_ids):
    import jsonschema
    value = {key: config[key] for key in ("template", "bindings") if key in config}
    try:
        jsonschema.validate(value, REFERENCE_TEMPLATE_SCHEMA)
    except jsonschema.ValidationError as exc:
        raise ToolError("Invalid reference template binding: " + exc.message, code="invalid_configuration") from exc
    bound = {identifier for binding in value["bindings"].values() for identifier in binding["chart_ids"]}
    for binding in value['bindings'].values():
        geography = binding.get('geography', {})
        if geography:
            bound.add(geography['source_chart_id'])
            bound.update(source['chart_id'] for source in geography.get('administrative_boundaries', []))
    if bound != set(chart_ids):
        raise ToolError("Reference bindings must include every accessible source chart and no other charts.", code="invalid_configuration")
    return value


def build_reference_document(publication_id=None, title="SINCEP supplied master", bindings=None):
    source = MASTER.read_bytes()
    if sha256(source).hexdigest() != MASTER_SHA256:
        raise ToolError("The supplied master template fingerprint has changed.", code="template_integrity")
    html = source.decode()
    # No dependency or script from the artifact is fetched/executed implicitly.
    html = re.sub(r'<script src="[^"]+"></script>', '', html)
    html = html.replace('<title>Master HDSS dashboard</title>', '<title>' + escape(title) + '</title>')
    live = publication_id is not None
    payload = {"publication_id": str(publication_id) if live else None, "title": title,
               "bindings": bindings or {}, "slots": {key: {"label": label, "page": page, "kind": kind} for key, (label, page, kind) in REFERENCE_SLOTS.items()},
               "master_sha256": MASTER_SHA256, "mode": "live" if live else "illustrative_preview"}
    safe_json = json.dumps(payload).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    # The only executable script is our compiled adapter. The original x-dc
    # script stays inert; preview alone explicitly executes its trusted source.
    injection = '<link rel="stylesheet" href="/lifedata-ai/static/dashboards/reference.css"><script id="vima-reference-data" type="application/json">' + safe_json + '</script><script src="/lifedata-ai/static/dashboards/reference.js"></script>'
    return html.replace('</body>', injection + '</body>')
