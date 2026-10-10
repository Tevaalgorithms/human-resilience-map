#!/usr/bin/env bash
# Bounded real-source download. Raster enrichment is a separate off-host step.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ "$#" -ne 4 ]; then
  echo 'Usage: scripts/download_region.sh STATE_FIPS COUNTY_FIPS WEST,SOUTH,EAST,NORTH NEW_OUTPUT_DIR' >&2
  exit 2
fi
uv run hrm prep-census --state "$1" --counties "$2" --bbox="$3" --year 2023 --simplify 0.00005 --out "$4"
