"""Census data preparation: tract boundaries (TIGERweb, no key) + ACS indicators (free API key).

Produces the five-file source directory consumed by ``FileAdapter``. Heat, canopy and transit
columns are left blank for later steps, and ``centers.csv`` / ``candidates.csv`` are header-only
templates for the planning partner to fill in. A dataset with blank *required* indicators is
correctly rejected by ingestion validation until those are supplied.
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from resilience_map.core.config import get_settings
from resilience_map.core.geography import BoundingBox, validate_bbox

TIGERWEB = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Tracts_Blocks/MapServer/0/query"
)
ACS_URL = "https://api.census.gov/data/{year}/acs/acs5"
TIGERWEB_ACS2023 = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/tigerWMS_ACS2023/MapServer/8/query"
PAGE = 1000
MAX_REGION_TRACTS = 500
MAX_RESPONSE_BYTES = 20_000_000

AGE_65_PLUS_VARS = [f"B01001_{i:03d}E" for i in (*range(20, 26), *range(44, 50))]
ACS_VARS = ["B01003_001E", "B19013_001E", "B19013_001M", *AGE_65_PLUS_VARS]
_SENTINELS = {-666666666, -999999999, -888888888, -777777777, -555555555, -333333333, -222222222}

CENTERS_HEADER = [
    "center_id",
    "name",
    "capacity",
    "lon",
    "lat",
    "hours",
    "last_confirmed",
    "opening_status",
    "source",
    "provenance",
]
CANDIDATES_HEADER = [
    "site_id",
    "name",
    "capacity",
    "cost_usd",
    "lon",
    "lat",
    "accessible",
    "last_confirmed",
    "notes",
]
INDICATOR_COLUMNS = [
    "tract_id",
    "surface_temp_anomaly_c",
    "pct_age_65_plus",
    "median_income_usd",
    "median_income_moe_usd",
    "canopy_pct",
    "transit_access_index",
]


def _validate_fips(state: str, counties: list[str]) -> None:
    if not re.fullmatch(r"[0-9]{2}", state) or not counties:
        raise ValueError("Use two-digit state and three-digit county FIPS codes")
    if counties != ["*"] and any(not re.fullmatch(r"[0-9]{3}", c) for c in counties):
        raise ValueError("Use three-digit county FIPS codes (or '*' for an operator batch)")


def _get_json(client: httpx.Client, url: str, params: dict[str, Any]) -> Any:
    """Bound every provider response before JSON decoding; never log API keys."""
    with client.stream("GET", url, params=params) as response:
        if response.status_code >= 400:
            # HTTPStatusError includes the full request URL, including the Census API key.
            raise RuntimeError(f"Census provider request failed ({response.status_code})")
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError("Census response exceeds 20 MB; select a smaller region")
        data = json.loads(body)
    if isinstance(data, dict) and "error" in data:
        raise ValueError("Census provider returned an error; download is incomplete")
    return data


def _num(v: Any) -> float | None:
    """ACS values arrive as strings; negative sentinels mean 'not available'."""
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if x in _SENTINELS or (x < 0 and x <= -1e6) else x


def fetch_tracts(
    state: str,
    counties: list[str],
    client: httpx.Client | None = None,
    simplify_degrees: float = 0.0,
    bbox: BoundingBox | None = None,
) -> list[dict[str, Any]]:
    """GeoJSON features for the given counties (WGS84), paginated.

    ``counties=["*"]`` fetches the whole state. ``simplify_degrees`` (e.g. 0.00005, about 5 m)
    generalizes geometry server-side to keep statewide files manageable.
    """
    _validate_fips(state, counties)
    if bbox is not None:
        bbox = validate_bbox(bbox)
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(30, connect=5))
    try:
        county_list = ",".join(f"'{c}'" for c in counties)
        scope = (
            f"STATE='{state}'"
            if counties == ["*"]
            else (f"STATE='{state}' AND COUNTY IN ({county_list})")
        )
        feats: list[dict[str, Any]] = []
        offset = 0
        while True:
            data = _get_json(
                client,
                TIGERWEB_ACS2023 if bbox is not None else TIGERWEB,
                {
                    "where": scope,
                    "outFields": "GEOID,NAME,STATE,COUNTY",
                    "outSR": 4326,
                    "f": "geojson",
                    "orderByFields": "GEOID",
                    "resultOffset": offset,
                    "resultRecordCount": PAGE,
                    **({"maxAllowableOffset": simplify_degrees} if simplify_degrees else {}),
                    **(
                        {
                            "geometry": ",".join(map(str, bbox)),
                            "geometryType": "esriGeometryEnvelope",
                            "inSR": 4326,
                            "spatialRel": "esriSpatialRelIntersects",
                        }
                        if bbox is not None
                        else {}
                    ),
                },
            )
            batch = data["features"]
            if not isinstance(batch, list):
                raise ValueError("Census provider returned invalid tract features")
            feats += batch
            if bbox is not None and len(feats) > MAX_REGION_TRACTS:
                raise ValueError("Selected region exceeds 500 tracts; choose a smaller area")
            if len(batch) < PAGE and not data.get("exceededTransferLimit", False):
                return feats
            if not batch:
                raise ValueError("Census pagination is incomplete")
            offset += len(batch)
    finally:
        if own:
            client.close()


def fetch_acs(
    state: str, counties: list[str], year: int, api_key: str, client: httpx.Client | None = None
) -> dict[str, dict[str, float | None]]:
    """``GEOID -> {population, pct_age_65_plus, median_income_usd, median_income_moe_usd}``."""
    _validate_fips(state, counties)
    own = client is None
    client = client or httpx.Client(timeout=60)
    try:
        result: dict[str, dict[str, float | None]] = {}
        # The Census geography predicate accepts one county or county:* per request. Query each
        # intersecting county independently rather than inventing an unsupported comma syntax.
        for county in counties:
            rows = _get_json(
                client,
                ACS_URL.format(year=year),
                {
                    "get": ",".join(["NAME", *ACS_VARS]),
                    "for": "tract:*",
                    "in": [f"state:{state}", f"county:{county}"],
                    "key": api_key,
                },
            )
            result.update(parse_acs(rows))
        return result
    finally:
        if own:
            client.close()


def parse_acs(rows: list[list[str]]) -> dict[str, dict[str, float | None]]:
    head, body = rows[0], rows[1:]
    idx = {name: i for i, name in enumerate(head)}
    out: dict[str, dict[str, float | None]] = {}
    for row in body:
        geoid = f"{row[idx['state']]}{row[idx['county']]}{row[idx['tract']]}"
        pop = _num(row[idx["B01003_001E"]])
        ages = [_num(row[idx[v]]) for v in AGE_65_PLUS_VARS]
        older = None if any(a is None for a in ages) else sum(a for a in ages if a is not None)
        out[geoid] = {
            "population": pop,
            "pct_age_65_plus": None if not pop or older is None else round(100 * older / pop, 2),
            "median_income_usd": _num(row[idx["B19013_001E"]]),
            "median_income_moe_usd": _num(row[idx["B19013_001M"]]),
        }
    return out


def fetch_county_names(
    state: str, year: int, api_key: str, client: httpx.Client | None = None
) -> dict[str, str]:
    """``5-digit county GEOID -> 'Travis County'`` for every county in the state."""
    own = client is None
    client = client or httpx.Client(timeout=60)
    try:
        rows = _get_json(
            client,
            ACS_URL.format(year=year),
            {"get": "NAME", "for": "county:*", "in": f"state:{state}", "key": api_key},
        )
        return {f"{row[1]}{row[2]}": row[0].split(",")[0] for row in rows[1:]}
    finally:
        if own:
            client.close()


def _csv_bytes(header: list[str], rows: list[dict[str, Any]]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=header, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode()


def prepare_directory(
    out_dir: str,
    state: str,
    counties: list[str],
    year: int,
    api_key: str | None = None,
    client: httpx.Client | None = None,
    simplify_degrees: float = 0.0,
    bbox: BoundingBox | None = None,
) -> dict[str, Any]:
    """Write the source directory. Returns a short summary for the CLI to print."""
    _validate_fips(state, counties)
    if bbox is not None:
        bbox = validate_bbox(bbox)
        if year != 2023:
            raise ValueError(
                "Selected-region preparation currently pins ACS and boundaries to 2023"
            )
    out = Path(out_dir)
    if out.exists() and any(out.iterdir()):
        raise ValueError(
            "Choose an empty output directory; existing inventories must not be overwritten"
        )
    key = api_key or get_settings().census_api_key.get_secret_value()
    if not key:
        raise RuntimeError(
            "Set CENSUS_API_KEY (free: https://api.census.gov/data/key_signup.html) to fetch ACS data"
        )
    feats = fetch_tracts(state, counties, client, simplify_degrees, bbox)
    if not feats:
        raise RuntimeError(f"No tracts returned for state {state}, counties {counties}")
    selected_counties = sorted({f["properties"]["GEOID"][2:5] for f in feats})
    if bbox is not None and len(selected_counties) > 10:
        raise ValueError("Selected region exceeds 10 counties")
    acs = fetch_acs(state, selected_counties if bbox is not None else counties, year, key, client)

    features, indicators, no_acs = [], [], 0
    for f in feats:
        geoid = f["properties"]["GEOID"]
        a = acs.get(geoid)
        if a is None or a["population"] is None:
            no_acs += 1
        if bbox is not None and (a is None or a["population"] is None):
            raise ValueError(
                "Selected tract lacks ACS population; no guessed population is supplied"
            )
        pop = int((a or {}).get("population") or 0)
        features.append(
            {
                "type": "Feature",
                "geometry": f["geometry"],
                "properties": {
                    "tract_id": geoid,
                    "name": f["properties"]["NAME"],
                    "population": pop,
                },
            }
        )
        indicators.append(
            {
                "tract_id": geoid,
                **dict.fromkeys(INDICATOR_COLUMNS[1:], ""),
                **{k: ("" if v is None else v) for k, v in (a or {}).items() if k != "population"},
            }
        )
    now = datetime.now(UTC).isoformat(timespec="seconds")
    names_all = fetch_county_names(state, year, key, client)
    present = {f["properties"]["tract_id"][:5] for f in features}
    county_names = {k: v for k, v in names_all.items() if k in present}
    manifest = [
        {
            "counties": county_names,
            "name": "Census TIGERweb tract boundaries",
            "url": TIGERWEB_ACS2023 if bbox is not None else TIGERWEB,
            "license": "US Government work (public domain)",
            "observation_period": "2023 boundary vintage"
            if bbox is not None
            else "current TIGERweb vintage",
            "selection_bbox": bbox,
            "selection_rule": "full intersecting tracts; no clipped population"
            if bbox is not None
            else "county selection",
            "geographic_coverage": f"state {state}, counties {counties}",
            "indicators": [],
            "retrieved_at": now,
        },
        {
            "name": f"ACS {year} 5-year estimates (B01003, B01001, B19013)",
            "url": ACS_URL.format(year=year),
            "license": "US Government work (public domain)",
            "observation_period": f"ACS {year - 4}-{year}",
            "geographic_coverage": f"state {state}, counties {counties}",
            "indicators": ["pct_age_65_plus", "median_income_usd"],
            "retrieved_at": now,
        },
        {
            "kind": "cooling_inventory",
            "name": "Locally verified inventory not supplied",
            "inventory_status": "unknown",
            "geographic_coverage": "Selected region; completeness unknown",
            "indicators": [],
        },
    ]
    out.mkdir(parents=True, exist_ok=True)
    (out / "tracts.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": features})
    )
    (out / "indicators.csv").write_bytes(_csv_bytes(INDICATOR_COLUMNS, indicators))
    (out / "centers.csv").write_bytes(_csv_bytes(CENTERS_HEADER, []))
    (out / "candidates.csv").write_bytes(_csv_bytes(CANDIDATES_HEADER, []))
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return {
        "tracts": len(features),
        "tracts_without_acs_population": no_acs,
        "directory": str(out),
        "still_needed": [
            "surface_temp_anomaly_c",
            "canopy_pct",
            "transit_access_index (optional)",
            "centers.csv",
            "candidates.csv",
        ],
    }


def add_county_names(directory: str, state: str, year: int, api_key: str | None = None) -> int:
    """Backfill county names into an existing source directory's manifest. Returns count added."""
    key = api_key or get_settings().census_api_key.get_secret_value()
    if not key:
        raise RuntimeError("Set CENSUS_API_KEY to look up county names")
    d = Path(directory)
    gj = json.loads((d / "tracts.geojson").read_text())
    present = {f["properties"]["tract_id"][:5] for f in gj["features"]}
    names = {k: v for k, v in fetch_county_names(state, year, key).items() if k in present}
    manifest = json.loads((d / "manifest.json").read_text())
    manifest[0]["counties"] = names
    (d / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return len(names)
