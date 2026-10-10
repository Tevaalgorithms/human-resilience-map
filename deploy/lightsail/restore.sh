#!/usr/bin/env bash
# Scratch-stack restoration only; refuses the live Compose project.
set -euo pipefail
umask 077
cd "$(dirname "$0")"
backup=${1:?Specify absolute backup directory}
[[ "$backup" = /* ]] || exit 1
[[ "${COMPOSE_PROJECT_NAME:-}" == hrm-restore-* ]] || { echo "Use COMPOSE_PROJECT_NAME=hrm-restore-<date> on a scratch host" >&2; exit 1; }
(cd "$backup" && sha256sum --check SHA256SUMS)
docker compose up -d --wait db
docker compose exec -T db pg_restore -U resilience -d resilience --exit-on-error < "$backup/database.dump"
docker compose run --rm --no-deps -T --entrypoint tar worker -C /data/objects -xf - < "$backup/objects.tar"
docker compose --profile tools run --rm migrate
