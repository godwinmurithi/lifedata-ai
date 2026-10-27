"""Declarative map presentation; no executable HTML, URLs or CSS are accepted."""

from lifedata_ai.tools.base import ToolError

COLOR = {"type": "string", "pattern": "^#[0-9a-fA-F]{6}$"}
SYMBOL = {"type": "string", "enum": ["circle", "square", "diamond", "pin", "icon"]}
ICON = {
    "type": "string",
    "enum": ["none", "hospital", "clinic", "person", "home", "laboratory", "water", "school", "star"],
}
CATEGORY_STYLE = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "color": COLOR,
        "shape": SYMBOL,
        "icon": ICON,
        "radius": {"type": "number", "minimum": 3, "maximum": 32},
    },
}
MAP_STYLE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "basemap": {"enum": ["street", "satellite"]},
        "markers": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "shape": SYMBOL,
                "icon": ICON,
                "color": COLOR,
                "outline_color": COLOR,
                "outline_width": {"type": "number", "minimum": 0, "maximum": 6},
                "radius": {"type": "number", "minimum": 3, "maximum": 32},
                "opacity": {"type": "number", "minimum": 0.1, "maximum": 1},
                "category_field": {"type": "string", "minLength": 1, "maxLength": 120},
                "categories": {
                    "type": "object",
                    "maxProperties": 100,
                    "additionalProperties": CATEGORY_STYLE,
                },
            },
        },
        "boundaries": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "color": COLOR,
                "fill_color": COLOR,
                "selected_color": COLOR,
                "weight": {"type": "number", "minimum": 0.5, "maximum": 6},
                "fill_opacity": {"type": "number", "minimum": 0, "maximum": 0.8},
            },
        },
        "popup": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "title_field": {"type": "string", "minLength": 1, "maxLength": 120},
                "hide_empty": {"type": "boolean"},
                "show_select": {"type": "boolean"},
                "fields": {
                    "type": "array",
                    "maxItems": 20,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "field": {"type": "string", "minLength": 1, "maxLength": 120},
                            "label": {"type": "string", "minLength": 1, "maxLength": 100},
                            "format": {"enum": ["text", "number", "boolean"]},
                        },
                        "required": ["field", "label"],
                    },
                },
            },
        },
    },
}


def map_fields(style):
    popup = style.get("popup", {})
    return list(
        dict.fromkeys(
            filter(
                None,
                [
                    popup.get("title_field"),
                    style.get("markers", {}).get("category_field"),
                    *(f["field"] for f in popup.get("fields", [])),
                ],
            )
        )
    )


def validate_map_style(style):
    import jsonschema

    try:
        jsonschema.validate(style, MAP_STYLE_SCHEMA)
    except jsonschema.ValidationError as exc:
        raise ToolError("Invalid map style: " + exc.message, code="invalid_configuration") from exc
