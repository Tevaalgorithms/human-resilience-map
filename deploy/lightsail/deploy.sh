#!/usr/bin/env bash
# Build and roll out one commit on the Lightsail host. Run from ~/hrm/deploy/lightsail.
# Usage: ./deploy.sh <git-sha>
# Keeps the previous image tag so a failed health check rolls back automatically.
set -euo pipefail
cd "$(dirname "$0")"

sha="${1:?usage: deploy.sh <git-sha>}"
tag="hrm-app:${sha:0:12}"
test -f .env && test -f runtime.env && test -f database.env  # never created by the pipeline

current="$(sed -n 's/^APP_IMAGE=//p' .env)"
domain="$(sed -n 's/^APP_DOMAIN=//p' .env)"

set_image() {
  sed "s|^APP_IMAGE=.*|APP_IMAGE=$1|" .env > .env.new && mv .env.new .env
}
healthy() {
  for _ in $(seq 1 30); do
    if curl --fail --silent --max-time 5 "https://${domain}/healthz" > /dev/null; then return 0; fi
    sleep 5
  done
  return 1
}

echo "Building ${tag} (current: ${current})"
docker build -t "${tag}" ../..

set_image "${tag}"
docker compose --profile tools run --rm migrate
docker compose up -d --remove-orphans

if healthy; then
  echo "Deployed ${tag}"
  # Keep the last five images for rollback; remove older ones.
  docker images --format '{{.Repository}}:{{.Tag}}' hrm-app | tail -n +6 | xargs -r docker rmi || true
else
  echo "Health check failed; rolling back to ${current}" >&2
  set_image "${current}"
  docker compose up -d --remove-orphans
  exit 1
fi
