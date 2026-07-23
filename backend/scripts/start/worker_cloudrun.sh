#!/bin/bash
set -e -x

# Cloud Run entrypoint for the Celery worker.
#
# A Cloud Run *service* must listen on $PORT for its startup probe, but a Celery
# worker serves no HTTP. So we start a tiny health server in the background to
# satisfy the probe, then exec the worker in the foreground. Running the worker
# as the foreground process means its exit terminates the container, so Cloud
# Run restarts a crashed/OOM-killed worker.
#
# Deploy this service with `--min-instances=1 --no-cpu-throttling` so the worker
# keeps consuming from the broker even without inbound HTTP requests.

uv run python scripts/health_server.py &

exec uv run celery -A app.main:celery_app worker \
    --loglevel=info \
    --pool=threads \
    --prefetch-multiplier=1 \
    -Q default,sdk_sync,garmin_sync,webhook_sync
