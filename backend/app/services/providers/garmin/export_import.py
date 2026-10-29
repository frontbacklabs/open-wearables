"""Garmin account-export import.

The "Export Your Data" ZIP a user downloads from Garmin's Account Management
Center holds the same per-day summaries the Health API pushes as webhooks --
``DI-Connect-Wellness/*.json`` files per type plus a
``summarizedActivities.json`` under ``DI-Connect-Fitness/``. This adapter
replays those records through the same ``process_items_batch`` ingest the push
pipeline uses, so an export import needs no new normalization code and gets
identical dedupe/upsert behavior.

Export JSON files carry no ``userId`` (unlike webhook notifications), so the
adapter keys on the OW user the upload is scoped to rather than doing the
connection lookup the webhook handler performs.
"""

import io
import json
import logging
import zipfile
from collections.abc import Iterator
from typing import Any

from app.database import DbSession
from app.services.providers.garmin.data_247 import Garmin247Data
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)

# Safety caps against zip bombs. The mobile client uploads only the wellness
# JSONs it picked, so legit uploads stay far below these.
_ZIP_MAGIC = b"PK\x03\x04"
_MAX_MEMBER_BYTES = 100 * 1024 * 1024
_MAX_TOTAL_BYTES = 500 * 1024 * 1024
_MAX_NESTED_DEPTH = 1

# Ordered (lowercase-basename substring, summary_type) rules. Specific names
# first -- "moveIQ" and "summarizedActivities" must be matched before the
# generic "activities"/"sleep" needles.
_FILE_TYPE_RULES: list[tuple[str, str]] = [
    ("moveiq", "moveIQActivities"),
    ("summarizedactivities", "activities"),
    ("sleep", "sleeps"),
    ("hrv", "hrv"),
    ("stress", "stressDetails"),
    ("respiration", "allDayRespiration"),
    ("pulseox", "pulseox"),
    ("spo2", "pulseox"),
    ("bloodpressure", "bloodPressures"),
    ("bodycomp", "bodyComps"),
    ("weight", "bodyComps"),
    ("usermetrics", "userMetrics"),
    ("skintemp", "skinTemp"),
    ("snapshot", "healthSnapshot"),
    ("epoch", "epochs"),
    ("mct", "mct"),
    ("menstrual", "mct"),
    ("udsfile", "dailies"),
    ("dailies", "dailies"),
    ("activities", "activities"),
]


def summary_type_for(member_name: str) -> str | None:
    """Map an archive member name to a Garmin summary type, or None."""
    basename = member_name.rsplit("/", 1)[-1].lower()
    if not basename.endswith(".json"):
        return None
    for needle, summary_type in _FILE_TYPE_RULES:
        if needle in basename:
            return summary_type
    return None


def _iter_archives(blob: bytes, depth: int) -> Iterator[zipfile.ZipFile]:
    """Yield the top-level archive plus one level of nested member zips.

    Garmin nests additional archives under ``DI-Connect-Uploaded-Files/``;
    member-level recursion is depth-capped and each level shares the caller's
    byte budget implicitly through the member size cap.
    """
    zf = zipfile.ZipFile(io.BytesIO(blob))
    yield zf
    if depth >= _MAX_NESTED_DEPTH:
        return
    for info in zf.infolist():
        if info.filename.lower().endswith(".zip") and info.file_size <= _MAX_MEMBER_BYTES:
            try:
                inner = zf.read(info)
                yield from _iter_archives(inner, depth + 1)
            except (zipfile.BadZipFile, RuntimeError):
                log_structured(
                    logger,
                    "warning",
                    "Skipping unreadable nested archive",
                    provider="garmin",
                    member=info.filename,
                )


def iter_export_records(zip_bytes: bytes) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Yield ``(member_name, summary_type, record)`` for every recognized JSON.

    Each wellness file is a JSON array of records (or a dict wrapping one
    list). Records are yielded raw -- ``process_items_batch`` normalizes them
    per type exactly as it does webhook PUSH items.
    """
    total = 0
    for zf in _iter_archives(zip_bytes, 0):
        for info in zf.infolist():
            if info.is_dir() or info.file_size > _MAX_MEMBER_BYTES:
                continue
            total += info.file_size
            if total > _MAX_TOTAL_BYTES:
                raise ValueError("Export exceeds the maximum supported size")
            summary_type = summary_type_for(info.filename)
            if summary_type is None:
                continue
            yield from _json_records(zf, info)


def _json_records(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Yield records from one recognized JSON archive member."""
    summary_type = summary_type_for(info.filename)
    assert summary_type is not None  # caller filters unrecognized names
    try:
        payload = json.loads(zf.read(info))
    except (json.JSONDecodeError, UnicodeDecodeError, RuntimeError):
        log_structured(
            logger,
            "warning",
            "Skipping unparsable export member",
            provider="garmin",
            member=info.filename,
        )
        return
    for record in _records_of(payload):
        if isinstance(record, dict):
            yield info.filename, summary_type, record


def iter_file_records(name: str, contents: bytes) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Yield ``(source_name, summary_type, record)`` from one uploaded file.

    A file that starts with the ZIP magic is walked like an export archive; a
    ``*.json`` upload is parsed directly, taking its type from the filename.
    """
    if contents.startswith(_ZIP_MAGIC):
        yield from iter_export_records(contents)
        return
    summary_type = summary_type_for(name)
    if summary_type is None:
        return
    try:
        payload = json.loads(contents)
    except (json.JSONDecodeError, UnicodeDecodeError):
        log_structured(
            logger,
            "warning",
            "Skipping unparsable export file",
            provider="garmin",
            member=name,
        )
        return
    for record in _records_of(payload):
        if isinstance(record, dict):
            yield name, summary_type, record


def _records_of(payload: Any) -> list[Any]:
    """Unwrap a file payload into a record list."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        lists = [v for v in payload.values() if isinstance(v, list)]
        if len(lists) == 1:
            return lists[0]
        return [payload]
    return []


def run_export_import(
    db: DbSession,
    user_id: str,
    files: list[tuple[bytes, str]],
    data_247: Garmin247Data,
    trace_id: str,
) -> dict[str, Any]:
    """Group export records per summary type and ingest via the push path.

    ``files`` is ``(contents, filename)`` pairs; each is either a whole export
    ZIP or one JSON member. Returns per-type saved counts plus errors so the
    import's sync run reports exactly what landed.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for contents, name in files:
        for _member, summary_type, record in iter_file_records(name, contents):
            grouped.setdefault(summary_type, []).append(record)

    saved: dict[str, int] = {}
    errors: list[str] = []
    for summary_type, items in grouped.items():
        try:
            saved[summary_type] = data_247.process_items_batch(db, user_id, summary_type, items)
        except Exception as e:
            log_structured(
                logger,
                "error",
                "Export import failed for summary type",
                provider="garmin",
                trace_id=trace_id,
                summary_type=summary_type,
                error=str(e),
            )
            errors.append(f"{summary_type}: {e}")

    return {
        "saved": saved,
        "items": {k: len(v) for k, v in grouped.items()},
        "errors": errors,
        "types": sorted(grouped),
    }
