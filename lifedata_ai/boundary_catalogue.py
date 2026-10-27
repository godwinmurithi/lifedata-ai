"""Shared African reference catalogue. No client records or credentials are cached."""

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx
from django.conf import settings
from django.contrib.gis.geos import GEOSGeometry
from django.db import transaction

from lifedata_ai.models import BoundaryArea, BoundaryRelease

# 54 African UN member states. Territories are inventoried separately, not called countries.
AFRICAN_COUNTRIES = frozenset(
    "AGO BDI BEN BFA BWA CAF CIV CMR COD COG COM CPV DJI DZA EGY ERI ETH GAB GHA GIN GMB GNB GNQ KEN LBR LBY LSO MAR MDG MLI MOZ MRT MUS MWI NAM NER NGA RWA SDN SEN SLE SOM SSD STP SWZ SYC TCD TGO TUN TZA UGA ZAF ZMB ZWE".split()
)
AFRICAN_TERRITORIES = frozenset("ESH MYT REU SHN".split())
INDEX_URL = "https://www.geoboundaries.org/api/current/gbOpen/ALL/ALL/"


def cache_root():
    root = Path(
        getattr(settings, "BOUNDARY_CATALOGUE_CACHE", settings.BASE_DIR / ".boundary-cache")
    )
    root.mkdir(parents=True, exist_ok=True)
    return root


def download(url):
    # URLs are provider metadata, not user-defined fetch targets.
    if urlparse(url).scheme != "https" or urlparse(url).hostname not in {
        "www.geoboundaries.org",
        "github.com",
        "raw.githubusercontent.com",
        "media.githubusercontent.com",
    }:
        raise ValueError("Unsupported boundary source URL")
    with httpx.Client(follow_redirects=True, timeout=120) as client:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            chunks = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > 50_000_000:
                    raise ValueError("Boundary file exceeds import limit")
                chunks.append(chunk)
            return b"".join(chunks)


def source_index(refresh=False):
    path = cache_root() / "index.json"
    if refresh or not path.exists():
        data = download(INDEX_URL)
        if not isinstance(json.loads(data), list):
            raise ValueError("Invalid provider index")
        temp = path.with_suffix(".tmp")
        temp.write_bytes(data)
        temp.replace(path)
    return json.loads(path.read_bytes())


def label_for(meta):
    label = str(meta.get("boundaryCanonical") or "").strip()
    return label if label.lower() not in {"", "unknown", "nan"} else meta["boundaryType"]


def validate_features(payload):
    return list(iter_valid_features(payload))


def iter_valid_features(payload):
    if payload.get("type") != "FeatureCollection" or not payload.get("features"):
        raise ValueError("Expected nonempty FeatureCollection")
    seen = set()
    for feature in payload["features"]:
        props = feature.get("properties") or {}
        identifier, name = str(props.get("shapeID") or ""), str(props.get("shapeName") or "")
        if not identifier:
            raise ValueError("Missing source ID")
        # Provider IDs can repeat; retain the original ID in properties and use
        # a geometry-derived suffix only for duplicated feature identities.
        if identifier in seen:
            identifier += (
                "-"
                + hashlib.sha256(
                    json.dumps(feature.get("geometry"), sort_keys=True).encode()
                ).hexdigest()[:16]
            )
            if identifier in seen:
                continue
        if not name:
            name = "Not recorded"
        geometry = GEOSGeometry(json.dumps(feature.get("geometry")), srid=4326)
        if (
            geometry.geom_type not in {"Polygon", "MultiPolygon"}
            or geometry.empty
            or not geometry.valid
        ):
            raise ValueError(f"Invalid polygon: {identifier}")
        west, south, east, north = geometry.extent
        if not (-180 <= west <= east <= 180 and -90 <= south <= north <= 90):
            raise ValueError("Coordinates are not WGS84 longitude/latitude")
        # Analysis retains original polygons; simplification is display-only.
        display = geometry.simplify(0.005, preserve_topology=True)
        yield dict(
            source_id=identifier,
            area_name=name,
            geometry_geojson=json.loads(geometry.geojson),
            display_geojson=json.loads(display.geojson),
            bbox=list(geometry.extent),
            properties=props,
        )
        seen.add(identifier)


def import_layer(meta, retry=False):
    identifier = meta["boundaryID"]
    if not re.fullmatch(r"[A-Z]{3}-ADM[0-5]-[A-Za-z0-9-]+", identifier):
        raise ValueError("Unexpected provider layer identifier")
    old = BoundaryRelease.objects.filter(pk=identifier).first()
    if old and old.status == "ready":
        return old, False
    url = meta["gjDownloadURL"]
    release, _ = BoundaryRelease.objects.update_or_create(
        pk=identifier,
        defaults={
            "country_iso3": meta["boundaryISO"],
            "country_name": meta["boundaryName"],
            "admin_level": int(meta["boundaryType"][3:]),
            "label": label_for(meta),
            "represented_year": meta.get("boundaryYearRepresented", ""),
            "license": meta.get("boundaryLicense", ""),
            "metadata": meta,
            "attribution": "geoBoundaries / William & Mary; " + meta.get("boundarySource", ""),
            "source_url": url,
            "checksum": "",
            "status": "pending",
            "error": "",
        },
    )
    try:
        path = cache_root() / (identifier + ".geojson")
        if not path.exists() or retry:
            content = download(url)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(content)
            tmp.replace(path)
        else:
            content = path.read_bytes()
        checksum = hashlib.sha256(content).hexdigest()
        payload = json.loads(content)
        del content
        with transaction.atomic():
            release.areas.all().delete()
            batch = []
            count = 0
            for feature in iter_valid_features(payload):
                batch.append(BoundaryArea(release=release, **feature))
                count += 1
                if len(batch) >= 25:
                    BoundaryArea.objects.bulk_create(batch, batch_size=25)
                    batch = []
            if batch:
                BoundaryArea.objects.bulk_create(batch, batch_size=25)
            expected = int(meta.get("admUnitCount") or count)
            release.metadata = {
                **meta,
                "import_notes": {
                    "declared_feature_count": expected,
                    "imported_feature_count": count,
                    "count_discrepancy": expected != count,
                    "missing_names": sum(
                        not (f.get("properties") or {}).get("shapeName")
                        for f in payload["features"]
                    ),
                },
            }
            release.feature_count = count
            release.checksum = checksum
            release.status = "ready"
            release.error = ""
            release.save()
        return release, True
    except Exception as exc:
        release.status = "failed"
        release.error = str(exc)[:1000]
        release.save(update_fields=["status", "error"])
        raise


def catalogue_layers(region="", include_failed=False):
    # This is intentionally shared PUBLIC reference data, never a tenant dataset.
    query = BoundaryRelease.objects.all()
    if not include_failed:
        query = query.filter(status="ready")
    term = region.strip()
    if term and term.lower() not in {"africa", "all"}:
        from django.db.models import Q

        aliases = {
            "ivory coast": "CIV",
            "côte d’ivoire": "CIV",
            "cote d'ivoire": "CIV",
            "dr congo": "COD",
            "drc": "COD",
            "swaziland": "SWZ",
            "cape verde": "CPV",
        }
        query = query.filter(
            Q(country_iso3__iexact=aliases.get(term.lower(), term)) | Q(country_name__iexact=term)
        )
    return list(query.order_by("country_iso3", "admin_level", "-imported_at"))


def layer_summary(release):
    return {
        "release_id": release.pk,
        "country_iso3": release.country_iso3,
        "country": release.country_name,
        "admin_level": release.admin_level,
        "label": release.label,
        "reference_year": release.represented_year,
        "features": release.feature_count,
        "status": release.status,
        "error": release.error,
        "provider": release.provider,
        "license": release.license,
        "attribution": release.attribution,
        "source_url": release.source_url,
        "import_notes": release.metadata.get("import_notes", {}),
        "dataset_id": release.dataset_id,
        "geometry_url": f"/lifedata-ai/api/boundaries/{release.pk}/",
    }


def coverage_manifest():
    layers = catalogue_layers(include_failed=True)
    present = {(r.country_iso3, r.admin_level) for r in layers if r.status == "ready"}
    return {
        "countries_expected": len(AFRICAN_COUNTRIES),
        "countries_ready": len(
            {iso for iso, level in present if iso in AFRICAN_COUNTRIES and level == 0}
        ),
        "missing_core_levels": [
            {"country_iso3": iso, "admin_level": level}
            for iso in sorted(AFRICAN_COUNTRIES)
            for level in (0, 1, 2)
            if (iso, level) not in present
        ],
        "layers": [layer_summary(r) for r in layers],
    }
