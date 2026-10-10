from __future__ import annotations

import numpy as np
import pytest
from rasterio.transform import from_origin
from shapely.geometry import box

from resilience_map.ingestion.canopy import compute_canopy, tile_boxes
from resilience_map.ingestion.heat import (
    ST_OFFSET_K,
    SceneRaster,
    anomalies,
    composite,
    st_to_celsius,
)
from resilience_map.ingestion.zonal import label_raster, zonal_sums

# 10x10 grid, 1 unit pixels, origin top-left at (0, 10)
T = from_origin(0, 10, 1, 1)
LEFT, RIGHT = box(0, 0, 5, 10), box(5, 0, 10, 10)


def test_label_raster_assigns_by_pixel_center_without_overlap():
    labels = label_raster([LEFT, RIGHT], (10, 10), T)
    assert set(np.unique(labels)) == {1, 2}
    assert (labels[:, :5] == 1).all() and (labels[:, 5:] == 2).all()


def test_zonal_sums_ignores_nan_and_counts_valid_fraction():
    labels = label_raster([LEFT, RIGHT], (10, 10), T)
    values = np.full((10, 10), 10.0)
    values[:, 5:] = 20.0
    values[:5, 5:] = np.nan  # half of RIGHT is invalid
    sums, counts, total = zonal_sums(labels, values, 2)
    assert (sums / counts).tolist() == [10.0, 20.0]
    assert counts.tolist() == [50, 25] and total.tolist() == [50, 50]


def test_st_to_celsius_scaling_and_masking():
    dn = np.array([[round((300.0 - ST_OFFSET_K) / 0.00341802), 0, 20000]], dtype="uint16")
    qa = np.array([[0, 0, 0]], dtype="uint16")
    c = st_to_celsius(dn, qa)
    assert c[0, 0] == pytest.approx(300.0 - 273.15, abs=0.01)
    assert np.isnan(c[0, 1])  # fill value
    assert np.isnan(c[0, 2])  # 149 + 68 K is far below the plausible range
    cloud = np.array([[0b1000]], dtype="uint16")  # cloud bit
    water = np.array([[0b10000000]], dtype="uint16")  # water bit
    ok_dn = np.array([[dn[0, 0]]], dtype="uint16")
    assert np.isnan(st_to_celsius(ok_dn, cloud)[0, 0]) and np.isnan(
        st_to_celsius(ok_dn, water)[0, 0]
    )


def scene(sid, left, right, right_valid_frac=1.0):
    arr = np.full((10, 10), np.nan)
    arr[:, :5] = left
    arr[:, 5:] = right
    if right_valid_frac < 1.0:
        arr[: int(10 * (1 - right_valid_frac)), 5:] = np.nan
    return SceneRaster(sid, arr, T, [LEFT, RIGHT], [0, 1])


def test_composite_uses_median_and_requires_min_scenes():
    res = composite(2, [scene("a", 30, 40), scene("b", 32, 41), scene("c", 100, 42)])
    assert res.lst_c == [32.0, 41.0]  # median is robust to the 100 outlier
    assert res.scene_counts == [3, 3]
    assert composite(2, [scene("a", 30, 40), scene("b", 31, 41)]).lst_c == [None, None]


def test_composite_drops_scenes_that_barely_cover_a_tract():
    scenes = [scene(s, 30, 40, right_valid_frac=0.3) for s in "abc"]  # RIGHT only 30% valid
    assert composite(2, scenes).lst_c == [30.0, None]


def test_anomalies_are_relative_to_study_area_median():
    assert anomalies([30.0, 32.0, 40.0, None]) == [-2.0, 0.0, 8.0, None]
    assert anomalies([None, None]) == [None, None]


def test_tile_boxes_cover_bounds_without_gaps():
    tiles = tile_boxes((0, 0, 0.5, 0.3), size=0.2)
    assert len(tiles) == 3 * 2
    assert min(t[0] for t in tiles) == 0 and max(t[2] for t in tiles) == 0.5
    assert max(t[3] for t in tiles) == pytest.approx(0.3)


def test_compute_canopy_aggregates_across_tiles_and_treats_255_as_nodata():
    geoms = [box(0, 0, 0.3, 0.2), box(0.3, 0, 0.6, 0.2), box(5, 5, 5.1, 5.1)]

    def fetch(bbox):
        x0, y0, x1, y1 = bbox
        if x0 > 1:
            return None  # outside the coverage
        n = 20
        px = (x1 - x0) / n
        arr = np.full((n, n), 20.0, dtype="float32")
        arr[:, : n // 2] = 60.0 if x0 < 0.3 else 20.0
        arr[0, 0] = 255.0  # no-data pixel must not count
        return arr, from_origin(x0, y1, px, (y1 - y0) / n)

    out = compute_canopy(geoms, fetch, workers=2)
    assert out[2] is None  # no data returned for that tract
    assert out[0] is not None and out[1] is not None and out[0] > out[1]
    assert 0 <= out[1] <= 100


def test_select_scenes_caps_per_tile_not_globally():
    from datetime import datetime, timedelta
    from types import SimpleNamespace

    from resilience_map.ingestion.heat import select_scenes

    base = datetime(2022, 6, 1)
    items = [
        SimpleNamespace(id=f"{p}-{r}-{k}", datetime=base + timedelta(days=k * 8),
                        properties={"landsat:wrs_path": p, "landsat:wrs_row": r})
        for (p, r) in ((27, 39), (28, 39), (29, 40)) for k in range(10)
    ]  # fmt: skip
    chosen = select_scenes(items, per_tile=4)
    by_tile = {}
    for it in chosen:
        by_tile.setdefault(
            (it.properties["landsat:wrs_path"], it.properties["landsat:wrs_row"]), []
        ).append(it)
    assert len(chosen) == 12 and all(
        len(v) == 4 for v in by_tile.values()
    )  # every tile represented
    first_tile = sorted(by_tile[(27, 39)], key=lambda i: i.datetime)
    assert (
        first_tile[0].datetime < first_tile[-1].datetime
    )  # spread across the season, not clustered


def test_anomalies_are_relative_to_each_county_not_the_whole_state():
    # a cool mountain county (~20 C) and a hot desert county (~44 C): comparing against the
    # statewide median would make every mountain tract look like -12 C
    lst = [19.0, 20.0, 21.0, 43.0, 44.0, 45.0, None]
    groups = ["04005", "04005", "04005", "04013", "04013", "04013", "04013"]
    from resilience_map.ingestion.heat import county_groups

    assert anomalies(lst, groups) == [-1.0, 0.0, 1.0, -1.0, 0.0, 1.0, None]
    assert county_groups(["04005000100", "99999-x", "synthetic"]) == ["04005", None, None]
