"""Inspect and edit a saved map without changing its data or page layout."""

import copy

from django.db import transaction

from lifedata_ai.map_style import MAP_STYLE_SCHEMA, map_fields, validate_map_style
from lifedata_ai.models import DashboardPublication
from lifedata_ai.publication_sources import publication_charts
from lifedata_ai.tools.base import Tool, ToolError, ToolMode
from lifedata_ai.tools.superset_read import _parse_json_field, _prepare_query_context


def available_map_fields(client, config):
    from lifedata_ai.administrative import hierarchy

    identifier = config.get("facility_chart_id") or config["source_chart_id"]
    chart = client.get_chart(identifier)
    context, note = _parse_json_field(chart.get("query_context"), "query_context")
    prepared = _prepare_query_context(context, chart, note, row_limit_cap=10000)
    dataset = client.get_dataset(prepared["datasource"]["id"])
    return (
        {c["column_name"] for c in dataset.get("columns", [])}
        | {"facility_name", "facility_level", "beds", "lat", "lng", "country", "location_basis"}
        | {level["field"] for level in hierarchy(config)}
    )


def merge_style(old, patch):
    result = copy.deepcopy(old)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_style(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


class EditDashboardMapStyleTool(Tool):
    name = "edit_dashboard_map_style"
    mode = ToolMode.READ
    description = (
        "Inspect or edit map popups, marker types/icons/colors/size, category-specific markers, "
        "boundary colors/fills and basemap in an existing custom workspace map. Call with publication_id alone "
        "first to inspect supported fields and current style, then pass a targeted style patch. "
        "popup.fields sets the ordered fields/labels/formats; title_field sets its heading. "
        "markers supports circle/square/diamond/pin/icon; icon hospital/clinic/person/home/laboratory/water/school/star. "
        "category_field and categories assign styles to actual recorded categories. "
        "Uses safe text-only popups. No HTML or external icons. Preserves sources, filters, page layout and other map settings. "
        "Also supports existing country-coverage site markers: these remain explicitly labelled country-centroid proxies, never actual coordinates. "
        "reset=true resets only map styling. Does not edit native Superset maps or claim unsupported surfaces were updated."
    )
    input_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "publication_id": {"type": "string", "format": "uuid"},
            "style": MAP_STYLE_SCHEMA,
            "reset": {"type": "boolean"},
        },
        "required": ["publication_id"],
    }

    def run(self, context, publication_id, style=None, reset=False):
        from django.core.exceptions import ValidationError

        with transaction.atomic():
            try:
                publication = (
                    DashboardPublication.objects.select_for_update()
                    .filter(pk=publication_id, owner_username=context.identity.username)
                    .first()
                )
            except (ValueError, ValidationError):
                raise ToolError("Invalid publication ID.", code="invalid_configuration") from None
            if not publication:
                raise ToolError("No publication owned by you exists.", code="not_found")
            client = context.superset_client()
            publication_charts(client, publication)
            config = copy.deepcopy(publication.configuration or {})
            if config.get("preset") != "buikwe-workspace":
                raise ToolError(
                    "Map style editing currently supports custom workspace maps.",
                    code="unsupported_surface",
                )
            coverage_map = bool(
                config.get("workspace", {}).get("site_field")
                and config.get("workspace", {}).get("country_field")
                and not any(
                    config.get(key)
                    for key in (
                        "boundary_chart_id",
                        "administrative_boundaries",
                        "boundary_catalogue",
                    )
                )
            )
            if not config.get("facility_chart_id") and not coverage_map:
                raise ToolError(
                    "Popup editing requires a facility coordinate map or the country-coverage site map.",
                    code="invalid_source",
                )
            fields = available_map_fields(client, config)
            current = config.get("map_style", {})
            previous_snapshot = {'title': publication.title, 'theme': publication.theme,
                                 'layout': publication.layout, 'configuration': publication.configuration}
            if style is not None or reset:
                updated = {} if reset else merge_style(current, style)
                validate_map_style(updated)
                missing = set(map_fields(updated)) - fields
                if missing:
                    raise ToolError(
                        "Popup or category fields are absent from the permitted map source: "
                        + ", ".join(sorted(missing)),
                        code="invalid_source",
                    )
                config["map_style"] = updated
                publication.configuration = config
                publication.save(update_fields=["configuration", "updated_at"])
                from lifedata_ai.quality import start_build
                build = start_build(context, publication, previous_snapshot=previous_snapshot)
                current = updated
            return {
                "type": "published_dashboard"
                if style is not None or reset
                else "map_style_targets",
                "title": publication.title,
                "chart_count": len(publication.layout),
                "publication_id": str(publication.id),
                "url": f"/lifedata-ai/dashboards/{publication.id}/",
                "build_id": str(build.id) if style is not None or reset else None,
                "quality_status": "draft" if style is not None or reset else None,
                "available_fields": sorted(fields),
                "location_mode": "actual_coordinates"
                if config.get("facility_chart_id")
                else "country_centroid_proxy",
                "map_style": current,
                "supported_shapes": MAP_STYLE_SCHEMA["properties"]["markers"]["properties"][
                    "shape"
                ]["enum"],
                "supported_icons": MAP_STYLE_SCHEMA["properties"]["markers"]["properties"]["icon"][
                    "enum"
                ],
            }
