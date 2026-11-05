"""Authenticated shared reference data. No tenant data is read by these endpoints."""

import hashlib

from django.http import JsonResponse
from django.shortcuts import render

from lifedata_ai.boundary_catalogue import catalogue_layers, coverage_manifest, layer_summary
from lifedata_ai.models import BoundaryRelease


def page(request):
    if request.method != "GET":
        return JsonResponse({"error": "method_not_allowed"}, status=405)
    report = coverage_manifest()
    return render(
        request,
        "lifedata_ai/boundaries.html",
        {"report": report, "ready": sum(r["status"] == "ready" for r in report["layers"])},
    )


def catalogue(request):
    if request.method != "GET":
        return JsonResponse({"error": "method_not_allowed"}, status=405)
    if request.GET.get("coverage") == "1":
        return JsonResponse(coverage_manifest())
    return JsonResponse(
        {"layers": [layer_summary(r) for r in catalogue_layers(request.GET.get("region", ""))]}
    )


def geometry(request, release_id):
    if request.method != "GET":
        return JsonResponse({"error": "method_not_allowed"}, status=405)
    release = BoundaryRelease.objects.filter(pk=release_id, status="ready").first()
    if not release:
        return JsonResponse({"error": "Boundary release unavailable."}, status=404)
    bbox = None
    try:
        if request.GET.get("bbox"):
            bbox = [float(v) for v in request.GET["bbox"].split(",")]
            if len(bbox) != 4 or not (
                -180 <= bbox[0] < bbox[2] <= 180 and -90 <= bbox[1] < bbox[3] <= 90
            ):
                raise ValueError()
    except ValueError:
        return JsonResponse({"error": "bbox must be west,south,east,north."}, status=400)
    features = []
    # Fetch only one pinned layer. Display geometries never replace analysis polygons.
    for area in release.areas.only("source_id", "area_name", "bbox", "display_geojson").iterator(
        chunk_size=100
    ):
        b = area.bbox
        if bbox and (b[2] < bbox[0] or b[0] > bbox[2] or b[3] < bbox[1] or b[1] > bbox[3]):
            continue
        features.append(
            {
                "type": "Feature",
                "id": area.source_id,
                "properties": {
                    "name": area.area_name,
                    "country_iso3": release.country_iso3,
                    "admin_level": release.admin_level,
                    "release_id": release.pk,
                },
                "geometry": area.display_geojson,
            }
        )
    response = JsonResponse(
        {"type": "FeatureCollection", "features": features, "reference": layer_summary(release)}
    )
    response["Cache-Control"] = "private, max-age=86400"
    response["ETag"] = (
        '"' + hashlib.sha256((release.checksum + str(bbox)).encode()).hexdigest() + '"'
    )
    return response
