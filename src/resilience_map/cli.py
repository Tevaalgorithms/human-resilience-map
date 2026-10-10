"""Command-line entry point: ``hrm <command>``."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from resilience_map.core.config import get_settings
from resilience_map.core.geography import parse_bbox
from resilience_map.core.logging import configure_logging


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="hrm", description="Human Resilience Map")
    sub = p.add_subparsers(dest="cmd", required=True)
    purge = sub.add_parser(
        "remove-synthetic-data", help="preview removal of fictional database datasets/jobs"
    )
    purge.add_argument("--apply", action="store_true", help="delete only is_synthetic records")
    sub.add_parser("migrate", help="apply database migrations")
    validate = sub.add_parser("validate-import", help="validate local source snapshot offline")
    validate.add_argument("--dir", required=True)
    ing = sub.add_parser("ingest", help="run an ingestion (publishes a validated dataset version)")
    ing.add_argument("--source", choices=["files"], default="files")
    prep = sub.add_parser(
        "prep-census", help="download tract boundaries + ACS into a source directory"
    )
    prep.add_argument("--state", required=True, help="2-digit state FIPS, e.g. 48")
    prep.add_argument(
        "--counties", required=True, help="comma-separated 3-digit county FIPS, e.g. 453,491"
    )
    prep.add_argument("--bbox", help="west,south,east,north; bounded selected-region download")
    prep.add_argument("--year", type=int, default=2023, help="ACS 5-year end year")
    prep.add_argument(
        "--simplify",
        type=float,
        default=0.0,
        help="simplify boundaries (degrees, e.g. 0.00005 ~ 5 m); use for statewide runs",
    )
    prep.add_argument("--out", required=True, help="output directory")
    cn = sub.add_parser(
        "prep-county-names", help="backfill county names into a prep-census directory"
    )
    cn.add_argument("--dir", required=True)
    cn.add_argument("--state", required=True)
    cn.add_argument("--year", type=int, default=2023)
    ra = sub.add_parser("prep-reanomaly", help="recompute heat anomalies per county from saved LST")
    ra.add_argument("--dir", required=True)
    enr = sub.add_parser(
        "prep-enrich", help="add Landsat heat + NLCD canopy to a prep-census directory"
    )
    enr.add_argument("--dir", required=True)
    enr.add_argument(
        "--years", default="2021,2022,2023", help="summers to composite, comma-separated"
    )
    enr.add_argument(
        "--scenes-per-tile", type=int, default=8, help="max Landsat scenes per WRS-2 tile"
    )
    enr.add_argument(
        "--decimate", type=int, default=1, help="read every Nth pixel (2 = 60 m); use for statewide"
    )
    enr.add_argument("--max-cloud", type=float, default=15.0, help="max scene cloud cover %%")
    enr.add_argument("--skip-heat", action="store_true")
    enr.add_argument("--skip-canopy", action="store_true")
    roads = sub.add_parser("prep-roads", help="off-host OSMnx road snapshot preparation")
    roads.add_argument("--dir", required=True)
    roads.add_argument(
        "--bbox", required=True, help="west,south,east,north including travel buffer"
    )
    roads.add_argument("--as-of", required=True, help="UTC OSM snapshot timestamp")
    transit = sub.add_parser(
        "prep-california-transit",
        help="derive a tract transit index from official Caltrans compiled GTFS stops",
    )
    transit.add_argument("--dir", required=True)
    county = sub.add_parser(
        "prep-california-county", help="create a county package from a California source directory"
    )
    county.add_argument("--source", required=True)
    county.add_argument("--out", required=True)
    county.add_argument("--county-fips", required=True)
    cooling = sub.add_parser(
        "prep-la-cooling", help="inspect/import the official LA County cooling layer"
    )
    cooling.add_argument("--dir", required=True)
    api = sub.add_parser("api", help="run the FastAPI service")
    api.add_argument("--host", default="0.0.0.0")  # noqa: S104
    api.add_argument("--port", type=int, default=8000)
    api.add_argument("--reload", action="store_true")
    fe = sub.add_parser("frontend", help="run the Dash frontend")
    fe.add_argument("--host", default="0.0.0.0")  # noqa: S104
    fe.add_argument("--port", type=int, default=8050)
    fe.add_argument("--debug", action="store_true")
    sub.add_parser("worker", help="run the analysis queue consumer")
    sub.add_parser("dispatcher", help="run the outbox dispatcher")
    args = p.parse_args(argv)

    os.environ.setdefault(
        "HRM_RUNTIME_ROLE",
        {"prep-census": "ingest", "prep-county-names": "ingest"}.get(
            args.cmd,
            args.cmd
            if args.cmd in {"api", "frontend", "worker", "dispatcher", "migrate", "ingest"}
            else "migrate",
        ),
    )
    if args.cmd in {"prep-census", "prep-county-names"}:
        os.environ["HRM_SSM_REQUIRE_CENSUS_KEY"] = "true"
    settings = get_settings()
    configure_logging(settings.log_level)
    log = logging.getLogger("hrm")

    if args.cmd == "migrate":
        from resilience_map.db.migrate import run_migrations

        log.info("migrations applied: %s", run_migrations() or "none (up to date)")
    if args.cmd == "ingest":
        from resilience_map.ingestion.run import ingest

        res = ingest(args.source)
        log.info("ingestion %s: version=%s", res.status, res.version)
        if res.status == "failed":
            log.error("validation errors: %s", res.report.get("errors"))
            return 1
    elif args.cmd == "remove-synthetic-data":
        from resilience_map.core.db import session_scope
        from resilience_map.repository.maintenance import remove_synthetic_data

        with session_scope() as session:
            log.info("synthetic cleanup: %s", remove_synthetic_data(session, apply=args.apply))
    elif args.cmd == "validate-import":
        from pathlib import Path

        from resilience_map.ingestion.pipeline import parse_snapshot
        from resilience_map.ingestion.sources import REQUIRED_FILES
        from resilience_map.verification.checks import verify_dataset

        directory = Path(args.dir)
        bundle, geoms = parse_snapshot(
            {
                name: (directory / name).read_bytes()
                for name in (*REQUIRED_FILES, "roads.json")
                if (directory / name).is_file()
            }
        )
        report = verify_dataset(bundle, geoms)
        log.info("inventory=%s; checks=%s", bundle.inventory_status, report.to_dict())
        return 0 if report.passed else 1
    elif args.cmd == "prep-census":
        from resilience_map.ingestion.census import prepare_directory

        summary = prepare_directory(
            args.out,
            args.state,
            args.counties.split(","),
            args.year,
            simplify_degrees=args.simplify,
            bbox=parse_bbox(args.bbox) if args.bbox else None,
        )
        log.info("census prep complete: %s", summary)
    elif args.cmd == "prep-county-names":
        from resilience_map.ingestion.census import add_county_names

        log.info("county names added: %d", add_county_names(args.dir, args.state, args.year))
    elif args.cmd == "prep-reanomaly":
        from resilience_map.ingestion.enrich import recompute_anomalies

        log.info("anomalies recomputed for %d tracts", recompute_anomalies(args.dir))
    elif args.cmd == "prep-enrich":
        from resilience_map.ingestion.enrich import enrich_directory

        log.info(
            "enrichment complete: %s",
            enrich_directory(
                args.dir,
                [int(y) for y in args.years.split(",")],
                args.scenes_per_tile,
                do_heat=not args.skip_heat,
                do_canopy=not args.skip_canopy,
                decimate=args.decimate,
                max_cloud=args.max_cloud,
            ),
        )
    elif args.cmd == "prep-roads":
        from resilience_map.ingestion.roads import prepare_roads

        log.info(
            "roads prepared: %s",
            prepare_roads(args.dir, tuple(float(v) for v in args.bbox.split(",")), args.as_of),
        )
    elif args.cmd == "prep-california-transit":
        from resilience_map.ingestion.california import add_california_transit

        log.info("California transit enrichment complete: %s", add_california_transit(args.dir))
    elif args.cmd == "prep-california-county":
        from resilience_map.ingestion.california import create_county_package

        log.info(
            "California county package complete: %s",
            create_county_package(args.source, args.out, args.county_fips),
        )
    elif args.cmd == "prep-la-cooling":
        from resilience_map.ingestion.california import add_la_cooling_inventory

        log.info("LA cooling source inspection complete: %s", add_la_cooling_inventory(args.dir))
    elif args.cmd == "api":
        import uvicorn

        uvicorn.run(
            "resilience_map.backend.main:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
            log_config=None,
        )
    elif args.cmd == "frontend":
        from resilience_map.frontend.app import create_dash_app

        create_dash_app().run(host=args.host, port=args.port, debug=args.debug)
    elif args.cmd == "worker":
        from resilience_map.workers.consumer import run_consumer

        run_consumer()
    elif args.cmd == "dispatcher":
        from resilience_map.workers.dispatcher import run_dispatcher

        run_dispatcher()
    return 0


if __name__ == "__main__":
    sys.exit(main())
