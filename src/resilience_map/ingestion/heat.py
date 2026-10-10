"""Land-surface-temperature (LST) anomaly per tract from Landsat 8/9 Collection 2 Level-2.

Method (documented so results are reproducible and auditable):
* daytime scenes, June 1 - Sept 15 of the chosen years, scene cloud cover < threshold;
* pixel mask: fill, cloud, cirrus, cloud shadow, snow and WATER removed (Collection 2 QA_PIXEL);
* ST_B10 DN -> Kelvin (x0.00341802 + 149.0) -> degrees C;
* per scene, per tract: mean of valid pixels, only if >= 50% of the tract's pixels are valid;
* per tract: MEDIAN across scenes (needs >= 3 scenes);
* anomaly = tract LST minus the median tract LST of its county (local urban-heat-island signal).
This is SURFACE temperature, not air temperature or heat index.
"""

from __future__ import annotations

import logging
import statistics
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from rasterio.transform import Affine
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

from resilience_map.ingestion.zonal import label_raster, zonal_sums

log = logging.getLogger(__name__)

ST_SCALE, ST_OFFSET_K = 0.00341802, 149.0
# QA_PIXEL bits to reject: fill(0) dilated cloud(1) cirrus(2) cloud(3) shadow(4) snow(5) water(7)
QA_REJECT = 0b10111111
MIN_VALID_FRACTION = 0.5
MIN_SCENES = 3
PLAUSIBLE_C = (-20.0, 85.0)


def st_to_celsius(dn: np.ndarray, qa: np.ndarray) -> np.ndarray:
    """Convert ST_B10 digital numbers to degrees C, NaN where masked or implausible."""
    c = dn.astype("float32") * np.float32(ST_SCALE) + np.float32(ST_OFFSET_K - 273.15)
    bad = (dn == 0) | ((qa & QA_REJECT) != 0) | (c < PLAUSIBLE_C[0]) | (c > PLAUSIBLE_C[1])
    return np.where(bad, np.nan, c)


@dataclass
class SceneRaster:
    scene_id: str
    celsius: np.ndarray  # NaN = invalid
    transform: Affine
    geoms: list[BaseGeometry]  # tract geometries in the raster CRS, aligned with ``indices``
    indices: list[int]


@dataclass
class HeatResult:
    lst_c: list[float | None]
    scene_counts: list[int]
    scenes_used: list[str] = field(default_factory=list)


@dataclass
class SceneMeans:
    """Per-tract means from ONE scene: tiny compared with the raster it came from."""

    scene_id: str
    means: list[
        tuple[int, float]
    ]  # (tract index, mean LST in C) for tracts valid enough in this scene


def scene_means(sc: SceneRaster, min_valid: float = MIN_VALID_FRACTION) -> SceneMeans:
    """Reduce a scene raster to per-tract means so the raster can be freed immediately."""
    labels = label_raster(sc.geoms, sc.celsius.shape, sc.transform)
    sums, counts, total = zonal_sums(labels, sc.celsius, len(sc.geoms))
    out = [
        (i, float(sums[k] / counts[k]))
        for k, i in enumerate(sc.indices)
        if total[k] > 0 and counts[k] / total[k] >= min_valid
    ]
    return SceneMeans(sc.scene_id, out)


def composite_means(n: int, results: Iterable[SceneMeans]) -> HeatResult:
    """Median across scenes per tract, streaming over ``SceneMeans`` (constant memory)."""
    per_tract: list[list[float]] = [[] for _ in range(n)]
    used: list[str] = []
    for r in results:
        for i, mean in r.means:
            per_tract[i].append(mean)
        if r.means:
            used.append(r.scene_id)
    lst = [round(statistics.median(v), 3) if len(v) >= MIN_SCENES else None for v in per_tract]
    return HeatResult(lst, [len(v) for v in per_tract], used)


def composite(
    n: int, scenes: Iterable[SceneRaster], min_valid: float = MIN_VALID_FRACTION
) -> HeatResult:
    return composite_means(n, (scene_means(s, min_valid) for s in scenes))


def county_groups(tract_ids: Sequence[str]) -> list[str | None]:
    """County key (first 5 digits of an 11-digit GEOID); None for non-Census identifiers."""
    return [t[:5] if len(t) == 11 and t.isdigit() else None for t in tract_ids]


def anomalies(
    lst: Sequence[float | None], groups: Sequence[str | None] | None = None
) -> list[float | None]:
    """LST minus the median LST of the tract's group (default: the whole study area).

    Grouping by county keeps a statewide dataset honest: a mountain county is compared with itself,
    not with the desert, so anomalies reflect the *local* urban heat island.
    """
    keys = list(groups) if groups is not None else [None] * len(lst)
    by_group: dict[str | None, list[float]] = {}
    for v, g in zip(lst, keys, strict=True):
        if v is not None:
            by_group.setdefault(g, []).append(v)
    ref = {g: statistics.median(vals) for g, vals in by_group.items()}
    return [None if v is None else round(v - ref[g], 3) for v, g in zip(lst, keys, strict=True)]


# ---------------------------------------------------------------------------------------------
# Planetary Computer I/O (network). Kept thin; the logic above is unit-tested without it.
# ---------------------------------------------------------------------------------------------
def search_scenes(
    bounds: tuple[float, float, float, float], years: Sequence[int], max_cloud: float = 15.0
) -> list[Any]:
    import planetary_computer
    import pystac_client

    cat = pystac_client.Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        modifier=planetary_computer.sign_inplace,
    )
    items: dict[str, Any] = {}
    for y in years:
        search = cat.search(
            collections=["landsat-c2-l2"], bbox=list(bounds), datetime=f"{y}-06-01/{y}-09-15",
            query={"eo:cloud_cover": {"lt": max_cloud}, "platform": {"in": ["landsat-8", "landsat-9"]}},
        )  # fmt: skip
        for it in search.items():
            items[it.id] = it
    return sorted(items.values(), key=lambda i: i.datetime)


def select_scenes(items: Sequence[Any], per_tile: int) -> list[Any]:
    """Keep at most ``per_tile`` scenes per Landsat WRS-2 tile, evenly spread through time.

    Capping per tile (not globally) keeps every part of a large region covered by enough scenes.
    """
    groups: dict[tuple[Any, Any], list[Any]] = {}
    for it in items:
        key = (it.properties.get("landsat:wrs_path"), it.properties.get("landsat:wrs_row"))
        groups.setdefault(key, []).append(it)
    chosen: list[Any] = []
    for grp in groups.values():
        grp.sort(key=lambda i: i.datetime)
        if len(grp) > per_tile:
            step = len(grp) / per_tile
            grp = [grp[int(i * step)] for i in range(per_tile)]
        chosen += grp
    return sorted(chosen, key=lambda i: i.datetime)


def read_scene(
    item: Any, geoms: Sequence[BaseGeometry], tree: STRtree, decimate: int = 1
) -> SceneRaster | None:
    """Read just the window of one Landsat scene that covers the tracts it overlaps."""
    import rasterio
    from pyproj import Transformer
    from rasterio.windows import Window, from_bounds
    from shapely.geometry import shape
    from shapely.ops import transform as shp_transform

    footprint = shape(item.geometry)
    idx = [int(i) for i in tree.query(footprint) if geoms[int(i)].intersects(footprint)]
    if not idx:
        return None
    env = {"GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR", "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
           "GDAL_HTTP_MAX_RETRY": "5", "GDAL_HTTP_RETRY_DELAY": "2"}  # fmt: skip
    with (
        rasterio.Env(**env),
        rasterio.open(item.assets["lwir11"].href) as st_ds,
        rasterio.open(item.assets["qa_pixel"].href) as qa_ds,
    ):
        tf = Transformer.from_crs(4326, st_ds.crs, always_xy=True).transform
        local = [shp_transform(tf, geoms[i]) for i in idx]
        minx = min(g.bounds[0] for g in local)
        miny = min(g.bounds[1] for g in local)
        maxx = max(g.bounds[2] for g in local)
        maxy = max(g.bounds[3] for g in local)
        win = from_bounds(minx, miny, maxx, maxy, st_ds.transform).round_offsets().round_lengths()
        win = win.intersection(Window(0, 0, st_ds.width, st_ds.height))
        if win.width < 2 or win.height < 2:
            return None
        dn = st_ds.read(1, window=win)
        qa = qa_ds.read(1, window=win)
        wtransform = st_ds.window_transform(win)
    return SceneRaster(item.id, st_to_celsius(dn, qa), wtransform, local, idx)


def compute_heat(
    geoms: Sequence[BaseGeometry],
    years: Sequence[int],
    scenes_per_tile: int = 8,
    workers: int = 3,
    progress: Callable[[int, int], None] | None = None,
    decimate: int = 1,
    max_cloud: float = 15.0,
) -> HeatResult:
    """``decimate=2`` reads every 2nd pixel (60 m): ~4x less data, for statewide runs."""
    xs = [g.bounds for g in geoms]
    bounds = (
        min(b[0] for b in xs),
        min(b[1] for b in xs),
        max(b[2] for b in xs),
        max(b[3] for b in xs),
    )
    found = search_scenes(bounds, years, max_cloud)
    items = select_scenes(found, scenes_per_tile)
    if not items:
        raise RuntimeError("No suitable Landsat scenes found; relax cloud cover or widen the years")
    log.info("reading %d Landsat scenes (of %d found)", len(items), len(found))
    tree = STRtree(list(geoms))
    done = 0

    def work(item: Any) -> SceneMeans | None:
        try:
            sc = read_scene(item, geoms, tree, decimate)
            return None if sc is None else scene_means(sc)  # raster is freed when this returns
        except Exception:
            log.exception("skipping scene %s", item.id)
            return None

    results: list[SceneMeans] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for r in pool.map(work, items):
            done += 1
            if progress:
                progress(done, len(items))
            if r is not None:
                results.append(r)
    return composite_means(len(geoms), results)
