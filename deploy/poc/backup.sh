#!/usr/bin/env bash
set -euo pipefail
umask 077
cd "$(dirname "$(readlink -f "$0")")"
bucket=$(python3 -c 'import json; print(json.load(open("release.json"))["bucket"])')
region=$(python3 -c 'import json; print(json.load(open("release.json"))["region"])')
key="backups/$(date -u +%Y-%m-%dT%H-%M-%SZ)-$(cat /proc/sys/kernel/random/uuid).dump"
# Custom-format pg_dump is a transaction-consistent backup, including checkpoints/sessions.
docker compose --env-file deployment.env -f compose.yml -f runtime.json exec -T db \
  pg_dump -U resilience -d resilience -Fc | aws s3 cp - "s3://$bucket/$key" --region "$region" --only-show-errors
