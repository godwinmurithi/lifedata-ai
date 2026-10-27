import json
from unittest.mock import Mock

import pytest
from django.test import RequestFactory

from lifedata_ai import boundary_catalogue as catalogue
from lifedata_ai.models import BoundaryRelease
from lifedata_ai.views import boundaries

pytestmark = pytest.mark.django_db


def feature(identifier="one", name="Area A", west=0):
    return {
        "type": "Feature",
        "properties": {"shapeID": identifier, "shapeName": name},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[west, 0], [west + 1, 0], [west + 1, 1], [west, 1], [west, 0]]],
        },
    }


def meta(identifier="UGA-ADM1-123"):
    return {
        "boundaryID": identifier,
        "boundaryISO": "UGA",
        "boundaryName": "Uganda",
        "boundaryType": "ADM1",
        "boundaryCanonical": "Regions",
        "boundaryYearRepresented": "2020",
        "boundaryLicense": "CC BY 4.0",
        "boundarySource": "Example source",
        "admUnitCount": "1",
        "gjDownloadURL": "https://github.com/example/data.geojson",
    }


def test_import_is_immutable_and_failed_refresh_does_not_remove_ready_release(
    tmp_path, monkeypatch, settings
):
    settings.BOUNDARY_CATALOGUE_CACHE = tmp_path
    download = Mock(
        return_value=json.dumps({"type": "FeatureCollection", "features": [feature()]}).encode()
    )
    monkeypatch.setattr(catalogue, "download", download)
    release, changed = catalogue.import_layer(meta())
    assert changed and release.feature_count == 1 and release.status == "ready"
    assert catalogue.import_layer(meta())[1] is False
    download.assert_called_once()
    download.side_effect = ValueError("network unavailable")
    with pytest.raises(ValueError):
        catalogue.import_layer(meta("UGA-ADM1-456"))
    release.refresh_from_db()
    assert release.areas.count() == 1 and release.status == "ready"
    assert len(catalogue.catalogue_layers("Uganda")) == 1


def test_bad_geometry_rolls_back_all_partial_features(tmp_path, monkeypatch, settings):
    settings.BOUNDARY_CATALOGUE_CACHE = tmp_path
    bad = feature("bad")
    bad["geometry"]["coordinates"] = [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]]
    monkeypatch.setattr(
        catalogue,
        "download",
        lambda url: json.dumps(
            {"type": "FeatureCollection", "features": [feature(), bad]}
        ).encode(),
    )
    with pytest.raises(ValueError):
        catalogue.import_layer(meta())
    r = BoundaryRelease.objects.get()
    assert r.status == "failed" and r.areas.count() == 0


def test_missing_names_and_duplicate_provider_ids_are_disclosed_and_retained():
    rows = catalogue.validate_features(
        {"type": "FeatureCollection", "features": [feature(), feature(name="", west=2)]}
    )
    assert len(rows) == 2 and rows[0]["source_id"] != rows[1]["source_id"]
    assert rows[1]["area_name"] == "Not recorded" and rows[1]["properties"]["shapeID"] == "one"
    bad = feature()
    bad["geometry"]["coordinates"][0][0] = [200, 0]
    bad["geometry"]["coordinates"][0][-1] = [200, 0]
    with pytest.raises(ValueError):
        catalogue.validate_features({"type": "FeatureCollection", "features": [bad]})


def test_geometry_endpoint_is_pinned_filtered_and_carries_attribution(
    tmp_path, monkeypatch, settings
):
    settings.BOUNDARY_CATALOGUE_CACHE = tmp_path
    monkeypatch.setattr(
        catalogue,
        "download",
        lambda url: json.dumps({"type": "FeatureCollection", "features": [feature()]}).encode(),
    )
    r, _ = catalogue.import_layer(meta())
    factory = RequestFactory()
    result = boundaries.geometry(factory.get("/"), r.pk)
    payload = json.loads(result.content)
    assert payload["reference"]["release_id"] == r.pk and payload["reference"]["attribution"]
    assert len(payload["features"]) == 1
    assert (
        json.loads(boundaries.geometry(factory.get("/?bbox=10,10,11,11"), r.pk).content)["features"]
        == []
    )
    assert boundaries.geometry(factory.get("/?bbox=nan,0,1,1"), r.pk).status_code == 400
    assert boundaries.geometry(factory.get("/"), "unknown").status_code == 404
    report = catalogue.coverage_manifest()
    assert report["countries_expected"] == 54 and report["countries_ready"] == 0


def test_unknown_country_does_not_return_another_countrys_layer():
    BoundaryRelease.objects.create(
        id="UGA-ADM0-123",
        country_iso3="UGA",
        country_name="Uganda",
        admin_level=0,
        label="Country",
        checksum="a",
        status="ready",
    )
    assert catalogue.catalogue_layers("Kenya") == []
    assert catalogue.catalogue_layers("UGA")[0].country_name == "Uganda"


def test_catalogue_layers_enrich_workspace_and_drive_cascading_filters(
    tmp_path, monkeypatch, settings
):
    from lifedata_ai.middleware import SupersetIdentity
    from lifedata_ai.models import DashboardPublication
    from lifedata_ai.tests.test_administrative import setup_sources
    from lifedata_ai.views import workspace

    settings.BOUNDARY_CATALOGUE_CACHE = tmp_path
    country = feature("country", "Uganda")
    country["geometry"]["coordinates"] = [[[0, 0], [2, 0], [2, 1], [0, 1], [0, 0]]]
    payloads = [
        {"type": "FeatureCollection", "features": [country]},
        {
            "type": "FeatureCollection",
            "features": [feature("a", "District A"), feature("b", "District B", west=1)],
        },
    ]
    monkeypatch.setattr(catalogue, "download", lambda url: json.dumps(payloads.pop(0)).encode())
    country_meta = meta("UGA-ADM0-123")
    country_meta.update(boundaryType="ADM0", boundaryCanonical="Country")
    country_release, _ = catalogue.import_layer(country_meta)
    district_release, _ = catalogue.import_layer(meta())
    client, config, _ = setup_sources()
    config.pop("administrative_boundaries")
    config["boundary_catalogue"] = [
        {"release_id": country_release.pk, "field": "country", "label": "Country"},
        {"release_id": district_release.pk, "field": "district", "label": "District"},
    ]
    config["workspace"].update(
        selectors=["country", "district"],
        country_field="country",
        record_columns=["facility_name", "country", "district"],
    )
    publication = DashboardPublication.objects.create(
        title="Catalogue map", owner_username="ana", superset_dashboard_id=5, configuration=config
    )
    monkeypatch.setattr(workspace, "_client", lambda request: client)
    request = RequestFactory().post(
        "/",
        data=json.dumps({"criteria": {"district": ["District A"]}}),
        content_type="application/json",
    )
    request.superset_identity = SupersetIdentity(username="ana")
    response = workspace.data(request, publication.pk)
    assert response.status_code == 200, response.content
    result = json.loads(response.content)
    assert result["matches"] == 1 and result["rows"][0]["country"] == "Uganda"
    assert result["options"]["district"] == ["District A", "District B"]
    assert any("reference 2020" in n for n in result["geography"]["notes"])


def test_local_discovery_needs_no_external_project_search():
    from lifedata_ai.tools.geography import DiscoverAdministrativeBoundariesTool

    BoundaryRelease.objects.create(
        id="KEN-ADM1-123",
        country_iso3="KEN",
        country_name="Kenya",
        admin_level=1,
        label="Counties",
        checksum="a",
        status="ready",
    )
    context = Mock()
    result = DiscoverAdministrativeBoundariesTool().run(context, "Kenya")
    assert result["catalogue"][0]["label"] == "Counties"
    context.superset_client.assert_not_called()
