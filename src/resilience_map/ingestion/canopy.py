"""Tree canopy cover per tract from the USFS/NLCD Tree Canopy Cover product (MRLC WCS).

Source: NLCD Tree Canopy Cover CONUS v2021-4 (30 m, percent canopy 0-100; values > 100 are
no-data). Fetched in small WGS84 tiles and aggregated per tract (mean of valid pixels).
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor

import httpx
import numpy as np
from rasterio.io import MemoryFile
from rasterio.transform import Affine
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

from resilience_map.ingestion.zonal import label_raster, zonal_sums

log = logging.getLogger(__name__)
WCS_URL = "https://www.mrlc.gov/geoserver/mrlc_download/wcs"
COVERAGE = "mrlc_download__nlcd_tcc_conus_{year}_v2021-4"
TILE_DEG = 0.2

TileFetcher = Callable[[tuple[float, float, float, float]], tuple[np.ndarray, Affine] | None]


def wcs_tile_fetcher(year: int = 2021, client: httpx.Client | None = None) -> TileFetcher:
    http = client or httpx.Client(timeout=120)

    def fetch(bbox: tuple[float, float, float, float]) -> tuple[np.ndarray, Affine] | None:
        x0, y0, x1, y1 = bbox
        params: list[tuple[str, str | int | float | bool | None]] = [
            ("service", "WCS"), ("version", "2.0.1"), ("request", "GetCoverage"),
            ("coverageId", COVERAGE.format(year=year)),
            ("subset", f"Lat({y0},{y1})"), ("subset", f"Long({x0},{x1})"),
            ("subsettingcrs", "http://www.opengis.net/def/crs/EPSG/0/4326"),
            ("format", "image/geotiff"),
        ]  # fmt: skip
        for attempt in range(4):
            try:
                r = http.get(WCS_URL, params=params)
            except httpx.HTTPError:
                time.sleep(2**attempt)
                continue
            if r.status_code == 200 and "tiff" in r.headers.get("content-type", ""):
                with MemoryFile(r.content) as m, m.open() as ds:
                    return ds.read(1).astype("float32"), ds.transform
            if r.status_code < 500:  # no coverage here (e.g. ocean / outside CONUS)
                return None
            time.sleep(2**attempt)
        raise RuntimeError(f"WCS tile {bbox} failed after retries")

    return fetch


def tile_boxes(
    bounds: tuple[float, float, float, float], size: float = TILE_DEG
) -> list[tuple[float, float, float, float]]:
    x0, y0, x1, y1 = bounds
    nx, ny = math.ceil((x1 - x0) / size), math.ceil((y1 - y0) / size)
    return [
        (x0 + i * size, y0 + j * size, min(x0 + (i + 1) * size, x1), min(y0 + (j + 1) * size, y1))
        for i in range(nx)
        for j in range(ny)
    ]


def compute_canopy(
    geoms: Sequence[BaseGeometry],
    fetch: TileFetcher,
    workers: int = 4,
    progress: Callable[[int, int], None] | None = None,
) -> list[float | None]:
    """Mean canopy percent per tract (None where no valid pixel was found)."""
    n = len(geoms)
    xs = [g.bounds for g in geoms]
    bounds = (
        min(b[0] for b in xs),
        min(b[1] for b in xs),
        max(b[2] for b in xs),
        max(b[3] for b in xs),
    )
    tree = STRtree(list(geoms))
    sums, counts = np.zeros(n), np.zeros(n)
    tiles = tile_boxes(bounds)

    def work(tile: tuple[float, float, float, float]) -> tuple[list[int], np.ndarray, np.ndarray]:
        from shapely.geometry import box

        idx = [int(i) for i in tree.query(box(*tile))]
        if not idx:
            return [], np.zeros(0), np.zeros(0)
        got = fetch(tile)
        if got is None:
            return [], np.zeros(0), np.zeros(0)
        arr, transform = got
        values = np.where((arr >= 0) & (arr <= 100), arr, np.nan)
        labels = label_raster([geoms[i] for i in idx], arr.shape, transform)
        s, c, _ = zonal_sums(labels, values, len(idx))
        return idx, s, c

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for idx, s, c in pool.map(work, tiles):
            for k, i in enumerate(idx):
                sums[i] += s[k]
                counts[i] += c[k]
            done += 1
            if progress:
                progress(done, len(tiles))
    return [round(float(sums[i] / counts[i]), 2) if counts[i] > 0 else None for i in range(n)]
