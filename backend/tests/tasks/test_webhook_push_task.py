"""Tests for webhook → sync-log instrumentation in process_webhook_push.

The new-vs-updated split is derived centrally: a provider only has to return a
WriteCounts as its count, and the task surfaces the split — no per-provider
plumbing. Saves become SUCCESS runs, errors FAILED, no-ops SKIPPED, and
deliveries with no resolvable user (or from self-reporting Garmin) are dropped.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

import app.integrations.celery.tasks.webhook_push_task as task
from app.repositories.data_point_series_repository import WriteCounts
from app.schemas.sync_status import SyncStatus


@pytest.fixture
def user_id() -> str:
    # User IDs are Firebase UID strings, not UUIDs.
    return "xY2kLm9PvQrStUvWzAbC3dEf4gH5"


def _capture() -> tuple[list[dict], Any]:
    calls: list[dict] = []

    def fake(uid: Any, provider: str, **kw: Any) -> None:
        calls.append({"provider": provider, "user_id": str(uid), **kw})

    return calls, fake


def test_writecounts_split_surfaces_without_explicit_keys(user_id: str) -> None:
    """A WriteCounts count yields the new/updated split centrally (no records_inserted keys)."""
    calls, fake = _capture()
    with patch.object(task.sync_status_service, "emit_webhook_delivered", side_effect=fake):
        task._emit_webhook_sync_status(
            "oura",
            {
                "status": "processed",
                "data_type": "daily_activity",
                "records_saved": WriteCounts(0, 3),
                "user_id": user_id,
            },
        )
    assert len(calls) == 1
    c = calls[0]
    assert c["status"] == SyncStatus.SUCCESS
    assert c["items_processed"] == 3
    assert c["metadata"] == {"inserted": 0, "updated": 3}
    assert c["message"] == "webhook 0 new, 3 updated"


def test_plain_int_count_has_no_breakdown(user_id: str) -> None:
    """Workout-style providers returning a plain int still log a success run, no split."""
    calls, fake = _capture()
    with patch.object(task.sync_status_service, "emit_webhook_delivered", side_effect=fake):
        task._emit_webhook_sync_status("strava", {"status": "processed", "records_saved": 2, "user_id": user_id})
    assert calls[0]["status"] == SyncStatus.SUCCESS
    assert calls[0]["items_processed"] == 2
    assert calls[0]["metadata"] is None


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"status": "error", "error": "boom"}, SyncStatus.FAILED),
        ({"status": "ignored", "reason": "duplicate_activity"}, SyncStatus.SKIPPED),
        ({"status": "processed", "records_saved": 0}, SyncStatus.SKIPPED),  # saved nothing
    ],
)
def test_status_mapping(result: dict, expected: SyncStatus, user_id: str) -> None:
    calls, fake = _capture()
    with patch.object(task.sync_status_service, "emit_webhook_delivered", side_effect=fake):
        task._emit_webhook_sync_status("oura", {**result, "user_id": user_id})
    assert calls[0]["status"] == expected


def test_no_user_resolved_is_dropped() -> None:
    """user_not_found / invalid payloads carry no user_id → nothing to attribute."""
    calls, fake = _capture()
    with patch.object(task.sync_status_service, "emit_webhook_delivered", side_effect=fake):
        task._emit_webhook_sync_status("oura", {"status": "user_not_found", "oura_user_id": "x"})
    assert calls == []


def test_self_reporting_provider_excluded(user_id: str) -> None:
    """Garmin reports its own sync status; the shared task must not duplicate it."""
    calls, fake = _capture()
    with patch.object(task.sync_status_service, "emit_webhook_delivered", side_effect=fake):
        task._emit_webhook_sync_status("garmin", {"status": "processed", "saved_count": 9, "user_id": user_id})
    assert calls == []


def test_emission_never_raises() -> None:
    """Sync-log emission must never break webhook processing.

    A malformed (non-dict) result is dropped, but a non-UUID user_id string —
    Firebase UIDs are not UUIDs — is still attributed and emitted.
    """
    calls, fake = _capture()
    with patch.object(task.sync_status_service, "emit_webhook_delivered", side_effect=fake):
        task._emit_webhook_sync_status("oura", None)
        task._emit_webhook_sync_status("oura", {"status": "processed", "records_saved": 1, "user_id": "not-a-uuid"})
    assert len(calls) == 1
    assert calls[0]["user_id"] == "not-a-uuid"


def test_google_health_batch_emits_one_run_per_notification(user_id: str) -> None:
    """google_health returns per-notification entries under "results"; each attributed
    entry becomes its own run, and entries with no resolvable user are dropped."""
    calls, fake = _capture()
    result = {
        "status": "processed",
        "notifications": 3,
        "records_saved": 3,
        "results": [
            {"status": "processed", "data_type": "steps", "records_saved": 3, "user_id": user_id},
            {"status": "ignored", "reason": "no_interval", "user_id": user_id},
            {"status": "user_not_found", "health_user_id": "gh-user", "data_type": "sleep"},
        ],
    }
    with patch.object(task.sync_status_service, "emit_webhook_delivered", side_effect=fake):
        task._emit_webhook_sync_status("google_health", result)
    assert len(calls) == 2
    assert calls[0]["status"] == SyncStatus.SUCCESS
    assert calls[0]["items_processed"] == 3
    assert calls[0]["message"] == "webhook steps"
    assert calls[1]["status"] == SyncStatus.SKIPPED
    assert calls[1]["message"] == "skipped: no_interval"


def test_google_health_failed_notification_emits_failed_run(user_id: str) -> None:
    """A per-notification error attributed to a user becomes a FAILED run."""
    calls, fake = _capture()
    result = {
        "status": "processed",
        "notifications": 1,
        "records_saved": 0,
        "results": [
            {"status": "error", "error": "upstream 500", "data_type": "heart-rate", "user_id": user_id},
        ],
    }
    with patch.object(task.sync_status_service, "emit_webhook_delivered", side_effect=fake):
        task._emit_webhook_sync_status("google_health", result)
    assert len(calls) == 1
    assert calls[0]["status"] == SyncStatus.FAILED
    assert calls[0]["error"] == "upstream 500"
    assert calls[0]["message"] == "webhook heart-rate"


def test_batch_results_skip_malformed_entries(user_id: str) -> None:
    """A non-dict entry in a batch result is dropped without blocking the rest."""
    calls, fake = _capture()
    result = {
        "status": "processed",
        "results": [
            "junk",
            {"status": "processed", "records_saved": 1, "user_id": user_id},
        ],
    }
    with patch.object(task.sync_status_service, "emit_webhook_delivered", side_effect=fake):
        task._emit_webhook_sync_status("google_health", result)
    assert len(calls) == 1
    assert calls[0]["status"] == SyncStatus.SUCCESS
