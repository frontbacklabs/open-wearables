"""Garmin account-export import.

``POST /users/{user_id}/import/garmin/export`` accepts either the "Export Your
Data" ZIP from Garmin's Account Management Center or the individual
``*.json`` files a client selected out of it (the mobile app uploads only the
``DI-Connect-Wellness`` members). Processing is async on the ``garmin_sync``
queue; progress surfaces through sync-status like every other ingest.
"""

import base64
from typing import Annotated

from celery import current_app as celery_app
from fastapi import APIRouter, File, HTTPException, UploadFile, status

from app.database import DbSession
from app.repositories import UserConnectionRepository
from app.services import ApiKeyDep

router = APIRouter()

_PROCESS_EXPORT_TASK = "app.integrations.celery.tasks.garmin.export_task.process_garmin_export"


@router.post("/users/{user_id}/import/garmin/export", status_code=status.HTTP_202_ACCEPTED)
def import_garmin_export(
    user_id: str,
    db: DbSession,
    _api_key: ApiKeyDep,
    files: Annotated[list[UploadFile], File()],
) -> dict[str, str]:
    """Import Garmin export files for a connected user.

    Requires an active Garmin connection: imported records are attributed to
    the ``garmin`` provider, and a connection is what scopes that provenance.
    """
    # Celery's JSON serializer cannot carry bytes, so payloads go base64.
    encoded = []
    for upload in files:
        contents = upload.file.read()
        if not contents:
            continue
        encoded.append([base64.b64encode(contents).decode("ascii"), upload.filename or "upload"])
    if not encoded:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No files uploaded")

    connection = UserConnectionRepository().get_active_connection(db, user_id, "garmin")
    if connection is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No active Garmin connection for this user",
        )

    task = celery_app.send_task(
        _PROCESS_EXPORT_TASK,
        args=[encoded, user_id],
        queue="garmin_sync",
    )
    return {"status": "processing", "task_id": task.id, "user_id": user_id}
