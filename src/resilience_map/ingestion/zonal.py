"""Raster zonal statistics helpers (pure numpy; no network)."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from rasterio import features
from rasterio.transform import Affine
from shapely.geometry.base import BaseGeometry


def label_raster(
    geoms: Sequence[BaseGeometry], shape: tuple[int, int], transform: Affine
) -> np.ndarray:
    """Rasterize polygons to an int32 label grid: tract ``i`` -> label ``i + 1``, 0 = outside.

    Pixels are assigned by pixel *center* (``all_touched=False``), so tracts never overlap.
    """
    shapes = [(g, i + 1) for i, g in enumerate(geoms) if not g.is_empty]
    if not shapes:
        return np.zeros(shape, dtype=np.int32)
    return features.rasterize(shapes, out_shape=shape, transform=transform, fill=0, dtype="int32")


def zonal_sums(
    labels: np.ndarray, values: np.ndarray, n: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per-zone (sum of valid values, count of valid pixels, count of all pixels in the zone).

    ``values`` uses NaN for invalid pixels. Returns arrays of length ``n``.
    """
    flat_l = labels.ravel()
    inside = flat_l > 0
    all_counts = np.bincount(flat_l[inside], minlength=n + 1)[1 : n + 1]
    flat_v = values.ravel()
    ok = inside & np.isfinite(flat_v)
    sums = np.bincount(flat_l[ok], weights=flat_v[ok], minlength=n + 1)[1 : n + 1]
    counts = np.bincount(flat_l[ok], minlength=n + 1)[1 : n + 1]
    return sums, counts, all_counts
