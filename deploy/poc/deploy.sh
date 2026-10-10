#!/usr/bin/env bash
set -euo pipefail
umask 077
cd "$(dirname "$0")"
# The runner supplies non-secret release.json. The host fetches secrets locally.
python3 configure.py
compose=(docker compose --env-file deployment.env -f compose.yml -f runtime.json)
registry=$(python3 -c 'import json; print(json.load(open("release.json"))["app_image"].split("/")[0])')
region=$(python3 -c 'import json; print(json.load(open("release.json"))["region"])')
aws ecr get-login-password --region "$region" | docker login --username AWS --password-stdin "$registry"
"${compose[@]}" pull
"${compose[@]}" up -d --wait --wait-timeout 180 db
# Back up before migrations; imports are a separate operator-reviewed action.
./backup.sh
"${compose[@]}" stop worker dispatcher api frontend caddy
"${compose[@]}" --profile tools run --rm migrate
"${compose[@]}" up -d --wait --wait-timeout 240 db api frontend dispatcher worker caddy
"${compose[@]}" exec -T frontend python -c "import urllib.request; urllib.request.urlopen('http://localhost:8050/healthz', timeout=10)"
# Switch the backup pointer only after successful startup.
ln -sfn "$PWD" /opt/hrm/current
cat > /etc/systemd/system/hrm-backup.service <<'UNIT'
[Unit]
Description=Human Resilience Map PostgreSQL backup to S3
Requires=docker.service
After=docker.service network-online.target
[Service]
Type=oneshot
ExecStart=/opt/hrm/current/backup.sh
UNIT
cat > /etc/systemd/system/hrm-backup.timer <<'UNIT'
[Unit]
Description=Daily Human Resilience Map backup
[Timer]
OnCalendar=*-*-* 06:00:00 UTC
Persistent=true
RandomizedDelaySec=15m
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now hrm-backup.timer
# Keep recent bundles; runtime files contain secrets, so all releases remain root-only.
docker image prune -f >/dev/null
