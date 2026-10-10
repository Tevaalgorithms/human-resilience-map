#!/usr/bin/env bash
# Download + enrich + ingest real data. Requires CENSUS_API_KEY in the environment.
#   CENSUS_API_KEY=... scripts/load_real_data.sh
# Statewide runs read Landsat at 60 m (--decimate 2) with a per-tile scene cap to bound data volume.
set -u
cd "$(dirname "$0")/.."
: "${CENSUS_API_KEY:?set CENSUS_API_KEY (free: https://api.census.gov/data/key_signup.html)}"

ingest() {  # dir id name
  HRM_INGEST_DIR="$1" HRM_INGEST_STUDY_AREA_ID="$2" HRM_INGEST_STUDY_AREA_NAME="$3" \
    uv run hrm ingest --source files
}
step() { echo "=== $(date -u +%FT%TZ) $*"; }

step "Travis County TX: county names + re-ingest"
uv run hrm prep-county-names --dir data/travis --state 48 && \
  ingest data/travis travis-tx "Travis County, TX (real data)"

step "Arizona: names + enrich + ingest"
[ -d data/arizona ] || uv run hrm prep-census --state 04 --counties '*' --out data/arizona --simplify 0.00005
uv run hrm prep-county-names --dir data/arizona --state 04 && \
  uv run hrm prep-enrich --dir data/arizona --years 2021,2022,2023 --scenes-per-tile 4 --decimate 2 && \
  ingest data/arizona arizona-az "Arizona (real data)"

step "California: names + enrich (60 m, 6 summers) + ingest"
uv run hrm prep-county-names --dir data/california --state 06 && \
  uv run hrm prep-enrich --dir data/california --years 2019,2020,2021,2022,2023,2024 \
    --scenes-per-tile 4 --decimate 2 --max-cloud 20 && \
  ingest data/california california-ca "California (real data)"
step "done"
