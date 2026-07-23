#!/bin/bash
set -e -x

# Cloud Run entrypoint for Celery beat (the periodic-task scheduler).
#
# Like the worker, a Cloud Run *service* must listen on $PORT for its startup
# probe, but beat serves no HTTP. So we start a tiny health server in the
# background to satisfy the probe, then exec beat in the foreground. Running
# beat as the foreground process means its exit terminates the container, so
# Cloud Run restarts a crashed beat.
#
# Deploy this service with `--min-instances=1 --max-instances=1 --no-cpu-throttling`.
# Exactly ONE beat instance must ever run, otherwise scheduled tasks are
# enqueued multiple times.

uv run python scripts/health_server.py &

rm -f './celerybeat.pid'
exec uv run celery -A app.main:celery_app beat -l info
