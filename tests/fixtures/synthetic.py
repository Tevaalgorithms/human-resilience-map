"""Fictional data for offline tests only; excluded from the application package."""

from __future__ import annotations

import csv
import io
import json
import random

from resilience_map.ingestion.sources import Snapshot


def _csv(rows: list[dict[str, object]]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode()


class SyntheticAdapter:
    """Deterministic fictional metro (6x6 tract grid). NOT real data; for offline tests only."""

    LON0, LAT0, STEP, N = -97.80, 30.20, 0.02, 6

    def fetch(self) -> Snapshot:
        rng = random.Random(42)  # noqa: S311 - deterministic fixture, not security-sensitive
        features, indicators = [], []
        mid = (self.N - 1) / 2
        for r in range(self.N):
            for c in range(self.N):
                tid = f"99999{r:03d}{c:03d}"
                x0, y0 = self.LON0 + c * self.STEP, self.LAT0 + r * self.STEP
                ring = [
                    [x0, y0],
                    [x0 + self.STEP, y0],
                    [x0 + self.STEP, y0 + self.STEP],
                    [x0, y0 + self.STEP],
                    [x0, y0],
                ]
                features.append(
                    {
                        "type": "Feature",
                        "geometry": {"type": "Polygon", "coordinates": [ring]},
                        "properties": {
                            "tract_id": tid,
                            "name": f"Synthetic Tract {r}-{c}",
                            "population": rng.randint(2000, 7000),
                        },
                    }
                )
                core = 1 - (abs(r - mid) + abs(c - mid)) / (2 * mid)  # 1 at the centre
                indicators.append(
                    {
                        "tract_id": tid,
                        "surface_temp_anomaly_c": round(
                            1.0 + 5.5 * core + rng.uniform(-0.5, 0.5), 2
                        ),
                        "pct_age_65_plus": round(8 + 14 * (1 - core) * rng.uniform(0.6, 1.4), 1),
                        "median_income_usd": round(
                            35_000 + 70_000 * (1 - core) * rng.uniform(0.6, 1.3), -2
                        ),
                        "median_income_moe_usd": round(rng.uniform(4_000, 12_000), -2),
                        "canopy_pct": round(max(2.0, 38 * (1 - core) * rng.uniform(0.5, 1.2)), 1),
                        "transit_access_index": round(
                            min(1.0, 0.25 + 0.7 * core * rng.uniform(0.7, 1.1)), 2
                        ),
                    }
                )
        centers = [
            {
                "center_id": f"CTR-{i}",
                "name": n,
                "capacity": cap,
                "lon": round(lon, 4),
                "lat": round(lat, 4),
                "hours": "9-17 weekdays",
                "last_confirmed": "2026-06-15",
            }
            for i, (n, cap, lon, lat) in enumerate(
                [
                    ("Synthetic Library", 1500, -97.74, 30.26),
                    ("Synthetic Rec Center", 2200, -97.72, 30.22),
                    ("Synthetic Church Hall", 800, -97.78, 30.28),
                ],
                1,
            )
        ]
        sites = [
            {
                "site_id": f"SITE-{i}",
                "name": n,
                "capacity": cap,
                "cost_usd": cost,
                "lon": round(self.LON0 + (c + 0.5) * self.STEP, 4),
                "lat": round(self.LAT0 + (r + 0.5) * self.STEP, 4),
                "accessible": acc,
                "last_confirmed": "2026-07-01",
                "notes": "synthetic candidate",
            }
            for i, (n, cap, cost, r, c, acc) in enumerate(
                [
                    ("School Gym A", 3000, 250_000, 2, 2, True),
                    ("Community Hall B", 1800, 150_000, 3, 3, True),
                    ("Mall Atrium C", 4000, 500_000, 2, 3, True),
                    ("Clinic Annex D", 1200, 120_000, 0, 1, True),
                    ("Library Branch E", 2500, 300_000, 4, 0, True),
                    ("Church Hall F", 1500, 100_000, 1, 4, True),
                    ("Stadium Concourse G", 5000, 650_000, 3, 2, True),
                    ("Warehouse H", 2000, 90_000, 5, 5, False),
                ],
                1,
            )
        ]
        manifest = [
            {
                "name": "Synthetic tract boundaries",
                "url": "synthetic://generator",
                "license": "CC0",
                "observation_period": "n/a",
                "geographic_coverage": "Synthetic Metro",
                "indicators": [],
            },
            {
                "name": "Synthetic indicators",
                "url": "synthetic://generator",
                "license": "CC0",
                "observation_period": "2024-06/2024-08 (fictional)",
                "geographic_coverage": "Synthetic Metro",
                "indicators": [
                    "surface_temp_anomaly_c",
                    "pct_age_65_plus",
                    "median_income_usd",
                    "canopy_pct",
                    "transit_access_index",
                ],
            },
        ]
        files = {
            "tracts.geojson": json.dumps(
                {"type": "FeatureCollection", "features": features}, sort_keys=True
            ).encode(),
            "indicators.csv": _csv(indicators),
            "centers.csv": _csv(centers),
            "candidates.csv": _csv(sites),
            "manifest.json": json.dumps(manifest, sort_keys=True).encode(),
        }
        return Snapshot(
            "synthetic-metro",
            "Synthetic Metro (fictional)",
            True,
            files,
            "2026-01-01T00:00:00+00:00",
        )  # fixed => identical snapshot hash every run
