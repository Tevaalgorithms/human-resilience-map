from __future__ import annotations

import json

import httpx
import pytest

from resilience_map.ingestion.census import ACS_VARS, fetch_acs, parse_acs, prepare_directory

HEAD = ["NAME", *ACS_VARS, "state", "county", "tract"]


def row(pop, income, moe, older_each, tract="000100"):
    vals = {"B01003_001E": pop, "B19013_001E": income, "B19013_001M": moe}
    return ["T", *[vals.get(v, older_each) for v in ACS_VARS], "48", "453", tract]


def test_parse_acs_computes_pct_65_plus_and_geoid():
    out = parse_acs([HEAD, row("1000", "55000", "4000", "10")])
    r = out["48453000100"]
    assert r["population"] == 1000 and r["pct_age_65_plus"] == 12.0  # 12 age cells x 10
    assert r["median_income_usd"] == 55000 and r["median_income_moe_usd"] == 4000


def test_acs_sentinels_become_none_not_negative_numbers():
    out = parse_acs(
        [HEAD, row("500", "-666666666", "-555555555", "5"), row("0", "1", "1", "0", "000200")]
    )
    assert out["48453000100"]["median_income_usd"] is None
    assert out["48453000100"]["median_income_moe_usd"] is None
    assert out["48453000200"]["pct_age_65_plus"] is None  # zero population: no division by zero


def test_prepare_directory_writes_ingestible_files(tmp_path):
    poly = {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]}

    def handler(request: httpx.Request) -> httpx.Response:
        if "tigerweb" in request.url.host:
            return httpx.Response(
                200,
                json={
                    "features": [
                        {
                            "type": "Feature",
                            "geometry": poly,
                            "properties": {"GEOID": "48453000100", "NAME": "Census Tract 1"},
                        }
                    ]
                },
            )
        assert request.url.params["key"] == "k"
        if request.url.params["for"] == "county:*":
            return httpx.Response(
                200,
                json=[["NAME", "state", "county"], ["Travis County, Texas", "48", "453"]],
                headers={"content-type": "application/json"},
            )
        return httpx.Response(
            200,
            json=[HEAD, row("1000", "55000", "4000", "10")],
            headers={"content-type": "application/json"},
        )

    summary = prepare_directory(
        str(tmp_path),
        "48",
        ["453"],
        2023,
        "k",
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert summary["tracts"] == 1
    gj = json.loads((tmp_path / "tracts.geojson").read_text())
    assert gj["features"][0]["properties"] == {
        "tract_id": "48453000100",
        "name": "Census Tract 1",
        "population": 1000,
    }
    assert (tmp_path / "centers.csv").read_text().startswith("center_id")
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest[0]["counties"] == {"48453": "Travis County"}


def test_missing_key_gives_actionable_error(tmp_path, monkeypatch):
    monkeypatch.delenv("CENSUS_API_KEY", raising=False)
    monkeypatch.delenv("HRM_CENSUS_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="CENSUS_API_KEY"):
        prepare_directory(str(tmp_path), "48", ["453"], 2023)


def test_selected_region_uses_pinned_spatial_query_and_preserves_full_tracts(tmp_path):
    bounds = (-97.8, 30.2, -97.7, 30.3)
    poly = {
        "type": "Polygon",
        "coordinates": [
            [[-97.9, 30.1], [-97.9, 30.4], [-97.6, 30.4], [-97.6, 30.1], [-97.9, 30.1]]
        ],
    }

    def handler(request):
        if "tigerweb" in request.url.host:
            assert "tigerWMS_ACS2023/MapServer/8/query" in request.url.path
            assert request.url.params["geometry"] == ",".join(map(str, bounds))
            assert request.url.params["inSR"] == "4326"
            assert request.url.params["spatialRel"] == "esriSpatialRelIntersects"
            return httpx.Response(
                200,
                json={
                    "features": [
                        {
                            "geometry": poly,
                            "properties": {"GEOID": "48453000100", "NAME": "Tract 1"},
                        }
                    ]
                },
            )
        if request.url.params["for"] == "county:*":
            return httpx.Response(
                200, json=[["NAME", "state", "county"], ["Travis County, Texas", "48", "453"]]
            )
        assert request.url.params.get_list("in") == ["state:48", "county:453"]
        return httpx.Response(200, json=[HEAD, row("1000", "55000", "4000", "10")])

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        prepare_directory(str(tmp_path), "48", ["453"], 2023, "k", client, bbox=bounds)
    features = json.loads((tmp_path / "tracts.geojson").read_text())["features"]
    assert features[0]["geometry"] == poly  # no clipped geometry/full-population mismatch
    assert len((tmp_path / "centers.csv").read_text().splitlines()) == 1
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest[0]["selection_bbox"] == list(bounds)
    assert manifest[0]["observation_period"] == "2023 boundary vintage"
    assert manifest[-1]["inventory_status"] == "unknown"


@pytest.mark.parametrize(
    "state,counties,bbox,year",
    [
        ("48' OR 1=1", ["453"], None, 2023),
        ("48", ["453'"], None, 2023),
        ("48", ["453"], (0, 0, 1, 1), 2023),
        ("48", ["453"], (-97.8, 30.2, -97.7, 30.3), 2024),
    ],
)
def test_preparation_rejects_invalid_selection_before_network(
    tmp_path, state, counties, bbox, year
):
    with pytest.raises(ValueError):
        prepare_directory(str(tmp_path), state, counties, year, "fixture", bbox=bbox)
    assert not list(tmp_path.iterdir())


def test_preparation_never_overwrites_existing_local_inventory(tmp_path):
    inventory = tmp_path / "centers.csv"
    inventory.write_text("verified local inventory")
    with pytest.raises(ValueError, match="empty output directory"):
        prepare_directory(str(tmp_path), "48", ["453"], 2023, "fixture")
    assert inventory.read_text() == "verified local inventory"


def test_acs_queries_each_county_with_supported_geography_predicate():
    requested = []

    def handler(request):
        requested.append(request.url.params.get_list("in"))
        county = request.url.params.get_list("in")[1].split(":", 1)[1]
        values = row("1000", "55000", "4000", "10")
        values[-2] = county
        return httpx.Response(
            200,
            json=[HEAD, values],
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = fetch_acs("48", ["453", "491"], 2023, "k", client)
    assert requested == [
        ["state:48", "county:453"],
        ["state:48", "county:491"],
    ]
    assert len(result) == 2


def test_region_download_limits_and_provider_error_are_explicit(monkeypatch):
    from resilience_map.ingestion import census

    bounds = (-97.8, 30.2, -97.7, 30.3)
    monkeypatch.setattr(census, "MAX_REGION_TRACTS", 1)
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"features": [{}, {}]})
            )
        ) as client,
        pytest.raises(ValueError, match="500 tracts"),
    ):
        census.fetch_tracts("48", ["453"], client, bbox=bounds)
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"error": {"code": 500}})
            )
        ) as client,
        pytest.raises(ValueError, match="incomplete"),
    ):
        census.fetch_tracts("48", ["453"], client, bbox=bounds)
    monkeypatch.setattr(census, "MAX_RESPONSE_BYTES", 10)
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 11))
        ) as client,
        pytest.raises(ValueError, match="20 MB"),
    ):
        census.fetch_tracts("48", ["453"], client, bbox=bounds)


def test_provider_http_error_does_not_disclose_api_key():
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(403, text="denied"))
        ) as client,
        pytest.raises(RuntimeError) as error,
    ):
        fetch_acs("48", ["453"], 2023, "top-secret-fixture-key", client)
    assert "403" in str(error.value)
    assert "top-secret-fixture-key" not in str(error.value)
