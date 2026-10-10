"""Bounded directed multigraph Dijkstra routing over an immutable OSM export.

A standard-library adjacency list preserves every parallel edge and direction, avoiding
OSMnx's download/geospatial dependencies on a 4 GB host. OSMnx exports can be normalized
into this format offline; permission and mode-specific time weights must be explicit.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import time
from typing import Any

from resilience_map.core.models import DatasetBundle, TravelMode
from resilience_map.gis.access import DETOUR_FACTOR, SPEED_KMH, haversine_km, travel_minutes


class RoadRouter:
    def __init__(self, roads: dict[str, Any], mode: TravelMode) -> None:
        for required_key in (
            "source",
            "retrieved_at",
            "license",
            "coverage_bbox",
            "assumptions",
            "snap_limit_m",
        ):
            if not roads.get(required_key):
                raise ValueError(f"Road snapshot missing {required_key}")
        bounds = roads["coverage_bbox"]
        if len(bounds) != 4 or not all(math.isfinite(float(v)) for v in bounds):
            raise ValueError("Invalid road coverage bbox")
        west, south, east, north = map(float, bounds)
        if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
            raise ValueError("Invalid road coverage bbox")
        self.limit = float(roads["snap_limit_m"])
        if not math.isfinite(self.limit) or not 0 < self.limit <= 1000:
            raise ValueError("Snap limit must be 0-1000 metres")
        nodes, edges = roads["nodes"], roads["edges"]
        if len(nodes) > 50000 or len(edges) > 200000:
            raise ValueError("Road snapshot exceeds demonstration resource limits")
        self.nodes = {str(n["id"]): (float(n["lon"]), float(n["lat"])) for n in nodes}
        if not self.nodes or len(self.nodes) != len(nodes):
            raise ValueError("Empty or duplicate road nodes")
        if any(not (-180 <= x <= 180 and -90 <= y <= 90) for x, y in self.nodes.values()):
            raise ValueError("Invalid road coordinates")
        self.adj: dict[str, list[tuple[str, float]]] = {n: [] for n in self.nodes}
        keys = set()
        for e in edges:
            u, v = str(e["u"]), str(e["v"])
            key = (u, v, str(e["key"]))
            if u not in self.nodes or v not in self.nodes or key in keys:
                raise ValueError("Invalid edge endpoints or duplicate parallel edge key")
            keys.add(key)
            weights = e["minutes"]
            if not weights or any(
                m not in {"walk", "bike", "drive"} or not math.isfinite(float(w)) or float(w) <= 0
                for m, w in weights.items()
            ):
                raise ValueError("Edge weights must be finite positive mode-specific minutes")
            if mode in weights:
                self.adj[u].append((v, float(weights[mode])))
        if not any(self.adj.values()):
            raise ValueError(f"No routable edges for requested mode {mode}")
        self.snap_nodes = {u for u, edges in self.adj.items() if edges} | {
            v for edges in self.adj.values() for v, _ in edges
        }
        self.mode = mode
        self.hash = hashlib.sha256(
            json.dumps(roads, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def snap(self, lon: float, lat: float, deadline: float = math.inf) -> tuple[str | None, float]:
        best = (math.inf, "")
        for count, node in enumerate(self.snap_nodes):
            if count % 1000 == 0 and time.monotonic() > deadline:
                raise ValueError("Snapping exceeded 15 second matrix limit; reduce selected area")
            best = min(best, (haversine_km(lon, lat, *self.nodes[node]), node))
        km, node = best
        return (node if km * 1000 <= self.limit else None), km

    def distances(self, start: str, deadline: float = math.inf) -> dict[str, float]:
        d = {start: 0.0}
        queue = [(0.0, start)]
        iterations = 0
        while queue:
            iterations += 1
            if iterations % 1000 == 0 and time.monotonic() > deadline:
                raise ValueError(
                    "Routing preparation exceeded 15 second matrix limit; reduce selected area"
                )
            cost, u = heapq.heappop(queue)
            if cost != d[u]:
                continue
            for v, w in self.adj[u]:
                if cost + w < d.get(v, math.inf):
                    d[v] = cost + w
                    heapq.heappush(queue, (cost + w, v))
        return d


def access_matrix(
    bundle: DatasetBundle,
    mode: TravelMode,
    model: str,
    destinations: list[Any],
    speed_multiplier: float = 1.0,
) -> tuple[dict[tuple[str, str], float], list[dict[str, str]], dict[str, Any]]:
    if not math.isfinite(speed_multiplier) or not 0.5 <= speed_multiplier <= 1.5:
        raise ValueError("Routing speed multiplier must be from 0.5 to 1.5")
    if len(destinations) > 500:
        raise ValueError("Destination inventory exceeds 500 point demonstration limit")
    if len(bundle.tracts) * len(destinations) > 100000:
        raise ValueError("Access matrix exceeds 100000 pairs")
    router = None
    if model == "road_network":
        if bundle.roads is None:
            raise ValueError("Network routing requested but no pinned road snapshot is available")
        router = RoadRouter(bundle.roads, mode)
    elif model != "straight_line_proxy":
        raise ValueError("Unknown access model")
    deadline = time.monotonic() + 15
    matrix = {}
    issues = []
    targets = [(getattr(s, "site_id", None) or s.center_id, s) for s in destinations]
    snaps = {sid: router.snap(s.lon, s.lat, deadline) for sid, s in targets} if router else {}
    for t in bundle.tracts:
        if time.monotonic() > deadline:
            raise ValueError(
                "Accessibility preparation exceeded 15 second matrix limit; reduce selected area"
            )
        origin, connector = router.snap(t.lon, t.lat, deadline) if router else (None, 0.0)
        paths = router.distances(origin, deadline) if router and origin and destinations else {}
        for sid, s in targets:
            status = "reachable"
            if router:
                target, end_connector = snaps[sid]
                if origin is None or target is None:
                    minutes, status = math.inf, "unmatched"
                elif target not in paths:
                    minutes, status = math.inf, "unreachable"
                else:
                    # Off-road endpoint connections use assumed mode speed without detour factor.
                    minutes = (
                        paths[target] + (connector + end_connector) / SPEED_KMH[mode] * 60
                    ) / speed_multiplier
            else:
                minutes = travel_minutes(
                    haversine_km(t.lon, t.lat, s.lon, s.lat), mode, speed_multiplier
                )
            matrix[(t.tract_id, sid)] = minutes
            if status != "reachable":
                issues.append({"tract_id": t.tract_id, "destination_id": sid, "status": status})
    pin: dict[str, Any] = {
        "model": model,
        "mode": mode,
        "algorithm_version": "directed_dijkstra_v1" if router else "haversine_proxy_v1",
        "connector_speed_kmh": SPEED_KMH[mode] * speed_multiplier,
        "routing_speed_multiplier": speed_multiplier,
        "proxy_detour_factor": DETOUR_FACTOR if router is None else None,
        "dataset_version_id": bundle.dataset_version_id,
    }
    if router and bundle.roads is not None:
        pin.update(
            {
                "road_sha256": router.hash,
                "source": bundle.roads["source"],
                "retrieved_at": bundle.roads["retrieved_at"],
                "assumptions": bundle.roads["assumptions"],
                "snap_limit_m": router.limit,
                "connector": "straight endpoint connector at assumed mode speed",
            }
        )
    return matrix, issues, pin
