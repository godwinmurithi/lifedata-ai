import copy

import pytest

from lifedata_ai.map_style import validate_map_style
from lifedata_ai.middleware import SupersetIdentity
from lifedata_ai.models import DashboardPublication
from lifedata_ai.tests.test_administrative import setup_sources
from lifedata_ai.tools.base import ToolContext, ToolError
from lifedata_ai.tools.map_style import EditDashboardMapStyleTool

pytestmark = pytest.mark.django_db


def setup():
    client, config, _ = setup_sources()
    config["facility_chart_id"] = 12
    publication = DashboardPublication.objects.create(
        owner_username="ana",
        title="Map",
        configuration=config,
        layout=[
            {"chart_id": 12, "span": 12, "height": 480},
            {"chart_id": 13, "span": 6, "height": 320},
        ],
    )
    context = ToolContext(superset_session_cookie="fake", identity=SupersetIdentity(username="ana"))
    context._client = client
    return publication, context, client


def test_popup_marker_edits_preserve_data_filters_layout_and_other_style():
    publication, context, client = setup()
    original = copy.deepcopy(publication.configuration)
    layout = copy.deepcopy(publication.layout)
    tool = EditDashboardMapStyleTool()
    result = tool.run(context, str(publication.pk))
    assert "facility_name" in result["available_fields"]
    tool.run(
        context,
        str(publication.pk),
        style={
            "markers": {"shape": "icon", "icon": "hospital", "color": "#087f8c"},
            "popup": {
                "fields": [
                    {"field": "facility_name", "label": "Site"},
                    {"field": "ready", "label": "Readiness"},
                ]
            },
        },
    )
    tool.run(context, str(publication.pk), style={"markers": {"color": "#4167a4"}})
    publication.refresh_from_db()
    assert publication.layout == layout
    assert {k: v for k, v in publication.configuration.items() if k != "map_style"} == original
    style = publication.configuration["map_style"]
    assert style["markers"] == {"shape": "icon", "icon": "hospital", "color": "#4167a4"}
    assert style["popup"]["fields"][0]["label"] == "Site"
    with pytest.raises(ToolError, match="absent"):
        tool.run(context, str(publication.pk), style={"popup": {"title_field": "not_real"}})
    tool.run(context, str(publication.pk), reset=True)
    publication.refresh_from_db()
    assert publication.configuration["map_style"] == {}


def test_other_owner_cannot_edit_and_html_is_not_an_accepted_setting():
    publication, context, _ = setup()
    context.identity = SupersetIdentity(username="other")
    with pytest.raises(ToolError, match="owned"):
        EditDashboardMapStyleTool().run(
            context, str(publication.pk), style={"markers": {"radius": 8}}
        )
    for style in [
        {"popup": {"html": "<script>alert(1)</script>"}},
        {"markers": {"color": "url(evil)"}},
        {"markers": {"icon": "https://evil/icon.svg"}},
    ]:
        with pytest.raises(ToolError):
            validate_map_style(style)


def test_country_centroid_map_can_be_styled_without_a_facility_reference():
    publication, context, client = setup()
    config = publication.configuration
    config.pop("facility_chart_id")
    config.pop("administrative_boundaries")
    config["workspace"]["country_field"] = "country"
    publication.configuration = config
    publication.save()
    result = EditDashboardMapStyleTool().run(
        context,
        str(publication.pk),
        style={
            "markers": {"shape": "pin", "color": "#087f8c"},
            "popup": {
                "fields": [
                    {"field": "country", "label": "Country"},
                    {"field": "location_basis", "label": "Location basis"},
                ]
            },
        },
    )
    assert result["location_mode"] == "country_centroid_proxy"
    publication.refresh_from_db()
    assert publication.configuration["map_style"]["markers"]["shape"] == "pin"
