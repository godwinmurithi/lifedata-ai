"""JSON Schemas for copilot write actions (spec §6.1).

Re-exported here so callers can write `from lifedata_ai.validation.schemas
import get_schema` without knowing that the definitions currently live in a
single `actions` module — if the schemas are later split per domain, this stays
the stable import point.

Importing this package runs the import-time `check_schema` sweep in
`actions.py`, which is deliberate: a malformed schema should stop Django
starting, not surface mid-conversation.
"""

from __future__ import annotations

from lifedata_ai.validation.schemas.actions import (
    ACTION_SCHEMAS,
    ALLOWED_ACTION_TYPES,
    CHART_PARAMS_SCHEMA,
    COLOR_SCHEMES,
    JSON_SCHEMA_DIALECT,
    LAYOUT_PRESET_IDS,
    VIZ_ALLOWLIST,
    UnknownActionTypeError,
    get_schema,
)

__all__ = [
    "ACTION_SCHEMAS",
    "ALLOWED_ACTION_TYPES",
    "CHART_PARAMS_SCHEMA",
    "COLOR_SCHEMES",
    "JSON_SCHEMA_DIALECT",
    "LAYOUT_PRESET_IDS",
    "VIZ_ALLOWLIST",
    "UnknownActionTypeError",
    "get_schema",
]
