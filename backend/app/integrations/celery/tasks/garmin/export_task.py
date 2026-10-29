"""Celery task for Garmin account-export imports.

Queued by ``POST /users/{user_id}/import/garmin/export`` on the ``garmin_sync``
queue (the same queue live pushes use, so backfill/live ordering is preserved
for a given worker). The heavy lifting lives in
``services.providers.garmin.export_import`` -- this wrapper owns the session,
the sync-run events, and the failure reporting.
"""

import base64
from logging import getLogger
from typing import Any

from celery import shared_task

from app.database import SessionLocal
from app.schemas.sync_status import DataTypeKind, DataTypeOutcome, SyncScope, SyncSource, SyncStatus
from app.services.providers.factory import ProviderFactory
from app.services.providers.garmin.export_import import run_export_import
from app.services.sync_status_service import (
    emit_sync_completed,
    emit_sync_failed,
    emit_sync_started,
    new_run_id,
    run_status_from,
    try_record_data_types,
)
from app.utils.sentry_helpers import log_and_capture_error

log = getLogger(__name__)


def _outcomes(result: dict[str, Any]) -> list[DataTypeOutcome]:
    return [
        DataTypeOutcome(
            data_type=summary_type,
            kind=DataTypeKind.TASK,
            native_type=summary_type,
            status=SyncStatus.SUCCESS,
            reported_records=count,
        )
        for summary_type, count in result["items"].items()
    ]


@shared_task
def process_garmin_export(encoded_files: list[list[str]], user_id: str) -> dict[str, Any]:
    """Import Garmin export files for one user.

    ``encoded_files`` is a list of ``[base64_contents, filename]`` pairs -- each
    either a whole "Export Your Data" ZIP or one JSON member the client picked
    out of it. Base64 because Celery's JSON serializer cannot carry bytes.
    """
    filenames = [name for _contents, name in encoded_files]
    run_id = new_run_id(prefix="garmin_export")
    emit_sync_started(
        user_id,
        "garmin",
        SyncSource.GARMIN_EXPORT,
        scope=SyncScope.HISTORICAL,
        run_id=run_id,
        message=f"Importing Garmin export: {len(encoded_files)} files",
        metadata={"filenames": filenames},
    )

    files = [(base64.b64decode(contents), name) for contents, name in encoded_files]
    try:
        strategy = ProviderFactory().get_provider("garmin")
        with SessionLocal() as db:
            result = run_export_import(db, user_id, files, strategy.data_247, run_id)
            db.commit()
    except Exception as e:
        emit_sync_failed(
            user_id,
            "garmin",
            SyncSource.GARMIN_EXPORT,
            scope=SyncScope.HISTORICAL,
            run_id=run_id,
            error="Import failed",
            message="Garmin export import failed",
            metadata={"filenames": filenames},
        )
        log_and_capture_error(e, log, "Failed to import Garmin export", extra={"filenames": filenames})
        raise

    outcomes = _outcomes(result)
    status = run_status_from(outcomes)
    if result["errors"]:
        status = SyncStatus.PARTIAL
    emit_sync_completed(
        user_id,
        "garmin",
        SyncSource.GARMIN_EXPORT,
        scope=SyncScope.HISTORICAL,
        run_id=run_id,
        status=status,
        message=f"Garmin export import completed: {len(result['types'])} data types",
        items_processed=sum(result["items"].values()),
        metadata={"filenames": filenames, "saved": result["saved"], "errors": result["errors"]},
    )
    try_record_data_types(run_id, outcomes, scope=SyncScope.HISTORICAL)

    return {"user_id": user_id, "status": "success", "stats": result}
