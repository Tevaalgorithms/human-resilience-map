"""Optional off-host OSMnx acquisition into the small immutable routing format.

Runtime does not depend on OSMnx. Acquisition is an operator operation, never an analysis
fallback. Docs verified against OSMnx 2.1.1 user reference, 2026-10-09.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from resilience_map.core.models import TravelMode
from resilience_map.gis.access import SPEED_KMH
from resilience_map.gis.routing import RoadRouter
from resilience_map.gis.selection import validate_bbox


def normalize_graphs(
    graphs: dict[TravelMode, Any], bbox: tuple[float, ...], as_of: str
) -> dict[str, Any]:
    nodes = {}
    edges = []
    for mode, graph in graphs.items():
        for node, attrs in graph.nodes(data=True):
            nodes[str(node)] = {"id": str(node), "lon": attrs["x"], "lat": attrs["y"]}
        for u, v, key, attrs in graph.edges(keys=True, data=True):
            edges.append(
                {
                    "u": str(u),
                    "v": str(v),
                    "key": f"{mode}:{key}",
                    "minutes": {mode: float(attrs["length"]) / 1000 / SPEED_KMH[mode] * 60},
                    "osmid": attrs.get("osmid"),
                }
            )
    roads = {
        "source": "https://www.openstreetmap.org (OSMnx / Overpass)",
        "license": "ODbL 1.0; © OpenStreetMap contributors",
        "retrieved_at": datetime.now(UTC).isoformat(),
        "osm_as_of": as_of,
        "coverage_bbox": list(bbox),
        "snap_limit_m": 250,
        "assumptions": {
            "osmnx": "2.1.1",
            "topology": "unsimplified directed MultiDiGraph; all components retained; mode-specific OSMnx preset filters",
            "speed_kmh": SPEED_KMH,
            "restrictions": "OSMnx mode/oneway filtering; no turn restrictions, traffic, slope, crossing delays or heat-exposure costs",
            "buffer": "Importer must supply a box enclosing planning origins plus threshold-sized buffer",
        },
        "nodes": list(nodes.values()),
        "edges": edges,
    }
    for mode in graphs:
        RoadRouter(roads, mode)
    if len(json.dumps(roads).encode()) > 20_000_000:
        raise ValueError("Road export exceeds 20 MB")
    return roads


def prepare_roads(directory: str, bbox: tuple[float, ...], as_of: str) -> dict[str, Any]:
    box = validate_bbox(bbox)
    instant = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    if instant.tzinfo is None or instant > datetime.now(UTC):
        raise ValueError("OSM snapshot timestamp must be timezone-aware and in the past")
    import osmnx as ox

    if ox.__version__ != "2.1.1":
        raise ValueError("Use OSMnx 2.1.1 in the off-host preparation environment")
    ox.settings.requests_timeout = 60
    ox.settings.overpass_memory = 536870912
    date = instant.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    ox.settings.overpass_settings = f'[out:json][timeout:{{timeout}}]{{maxsize}}[date:"{date}"]'
    graphs = {
        mode: ox.graph_from_bbox(
            box, network_type=mode, simplify=False, retain_all=True, truncate_by_edge=False
        )
        for mode in SPEED_KMH
    }
    roads = normalize_graphs(graphs, box, date)
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    (out / "roads.json").write_text(json.dumps(roads, sort_keys=True))
    return {
        "nodes": len(roads["nodes"]),
        "edges": len(roads["edges"]),
        "path": str(out / "roads.json"),
    }
