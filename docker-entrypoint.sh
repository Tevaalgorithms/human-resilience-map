#!/bin/sh
set -eu
# Select the least-privilege SSM parameter set before Python loads its settings.
case "${1:-api}" in
  api|frontend|worker|dispatcher|migrate|ingest)
    export HRM_RUNTIME_ROLE="${HRM_RUNTIME_ROLE:-${1:-api}}" ;;
esac
case "${1:-api}" in
  api)               exec hrm api --host 0.0.0.0 --port "${PORT:-8000}" ;;
  frontend)          exec gunicorn "resilience_map.frontend.app:wsgi()" -b "0.0.0.0:${PORT:-8050}" \
                          --workers "${WEB_CONCURRENCY:-2}" --timeout 180 --access-logfile - ;;
  worker|dispatcher|migrate) exec hrm "$@" ;;
  ingest)            shift; exec hrm ingest "$@" ;;
  *)                 exec "$@" ;;
esac
