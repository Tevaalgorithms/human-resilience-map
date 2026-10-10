#!/usr/bin/env bash
# Back up DB + immutable source objects together. Keep encrypted copies off-host.
set -euo pipefail
umask 077
cd "$(dirname "$0")"
target=${1:?Specify a NEW absolute backup directory on encrypted storage}
[[ "$target" = /* && ! -e "$target" ]] || { echo "Expected new absolute directory" >&2; exit 1; }
mkdir -p "$target"
# Stop writers so the DB and local object snapshot describe the same point in time.
trap 'docker compose up -d api frontend worker dispatcher' EXIT
docker compose stop api frontend worker dispatcher
docker compose exec -T db pg_dump -U resilience -d resilience -Fc > "$target/database.dump"
docker compose run --rm --no-deps -T --entrypoint tar worker -C /data/objects -cf - . > "$target/objects.tar"
(cd "$target" && sha256sum database.dump objects.tar > SHA256SUMS)
