from __future__ import annotations

import csv
import json

from resilience_map.ingestion.california import (
    add_california_transit,
    add_la_cooling_inventory,
    create_county_package,
)


class Response:
    def __init__(self, features):
        self._payload = {"type": "FeatureCollection", "features": features}
        self.content = json.dumps(self._payload).encode()

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class Client:
    def __init__(self, features):
        self.features = features
        self.calls = []

    def get(self, url, params):
        self.calls.append((url, params))
        return Response(self.features)


def _source(tmp_path):
    features = []
    for tract_id, west in (("06037000100", 0), ("06037000200", 1), ("06059000300", 2)):
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [[west, 0], [west + 1, 0], [west + 1, 1], [west, 1], [west, 0]]
                    ],
                },
                "properties": {"tract_id": tract_id, "name": tract_id, "population": 100},
            }
        )
    (tmp_path / "tracts.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": features})
    )
    with (tmp_path / "indicators.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "tract_id",
                "surface_temp_anomaly_c",
                "pct_age_65_plus",
                "median_income_usd",
                "median_income_moe_usd",
                "canopy_pct",
                "transit_access_index",
            ],
        )
        writer.writeheader()
        for feature in features:
            writer.writerow({"tract_id": feature["properties"]["tract_id"]})
    (tmp_path / "manifest.json").write_text("[]")
    return features


def test_transit_index_uses_official_service_fields_and_records_provenance(tmp_path):
    _source(tmp_path)
    stops = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [0.5, 0.5]},
            "properties": {"OBJECTID": 1, "n_arrivals": 20, "n_hours_in_service": 10},
        },
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [1.5, 0.5]},
            "properties": {"OBJECTID": 2, "n_arrivals": 100, "n_hours_in_service": 10},
        },
    ]
    result = add_california_transit(str(tmp_path), Client(stops))
    rows = list(csv.DictReader((tmp_path / "indicators.csv").open()))
    values = {row["tract_id"]: float(row["transit_access_index"]) for row in rows}
    assert 0 < values["06037000100"] < values["06037000200"] == 1
    assert values["06059000300"] == 0
    assert result == {
        "tracts": 3,
        "provider_features": 2,
        "usable_stop_records": 2,
        "tracts_with_service": 2,
        "tracts_without_service": 1,
    }
    source = json.loads((tmp_path / "manifest.json").read_text())[0]
    assert source["indicators"] == ["transit_access_index"]
    assert "n_arrivals/n_hours_in_service" in source["method"]


def test_county_package_preserves_only_requested_real_tracts(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _source(source)
    output = tmp_path / "la"
    result = create_county_package(str(source), str(output), "06037")
    features = json.loads((output / "tracts.geojson").read_text())["features"]
    rows = list(csv.DictReader((output / "indicators.csv").open()))
    manifest = json.loads((output / "manifest.json").read_text())
    assert result["tracts"] == 2
    assert all(feature["properties"]["tract_id"].startswith("06037") for feature in features)
    assert len(rows) == 2
    assert manifest[-1]["inventory_status"] == "unknown"
    assert (output / "centers.csv").read_text().count("\n") == 1


def test_empty_official_cooling_layer_remains_unknown(tmp_path):
    _source(tmp_path)
    result = add_la_cooling_inventory(str(tmp_path), Client([]))
    assert result["provider_record_count"] == 0
    assert result["coverage_status"] == "downloaded_empty_unknown"
    assert "does not establish confirmed absence" in result["notice"]
    manifest = json.loads((tmp_path / "manifest.json").read_text())[-1]
    assert manifest["inventory_status"] == "unknown"
    assert manifest["provider_record_count"] == 0


def test_cooling_records_stay_partial_and_unknown_opening_status(tmp_path):
    _source(tmp_path)
    feature = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [0.5, 0.5]},
        "properties": {"OBJECTID": 7, "name": "Published Center", "hours": "10-5"},
    }
    result = add_la_cooling_inventory(str(tmp_path), Client([feature]))
    centers = list(csv.DictReader((tmp_path / "centers.csv").open()))
    assert result["imported_record_count"] == 1
    assert centers[0]["opening_status"] == "unknown"
    assert centers[0]["capacity"] == ""
    assert json.loads((tmp_path / "manifest.json").read_text())[-1]["inventory_status"] == "partial"


def test_cooling_provider_pagination_respects_thousand_record_limit():
    from resilience_map.ingestion.california import LA_COOLING_URL, _arcgis_pages

    class PagedClient:
        def __init__(self):
            self.offsets = []

        def get(self, url, params):
            assert url == LA_COOLING_URL
            assert params["resultRecordCount"] <= 1000
            self.offsets.append(params["resultOffset"])
            return Response(
                [{"properties": {"OBJECTID": i}} for i in range(1000)]
                if params["resultOffset"] == 0
                else [{"properties": {"OBJECTID": 1000}}]
            )

    client = PagedClient()
    records = _arcgis_pages(LA_COOLING_URL, "OBJECTID", client=client)
    assert len(records) == 1001 and client.offsets == [0, 1000]
