"""process_payload() attribution for failed Google webhook notifications.

A notification that raises mid-processing still resolves its OW user through the
connection lookup, so the error result carries ``user_id``/``data_type`` and the
shared webhook task can record a FAILED run for it.
"""

from unittest.mock import MagicMock, patch

from app.services.providers.google_health.webhook_handler import GoogleWebhookHandler

NOTIFICATION = {
    "data": {
        "healthUserId": "gh-user-1",
        "operation": "UPSERT",
        "dataType": "steps",
        "intervals": [
            {"physicalTimeInterval": {"startTime": "2026-01-01T00:00:00Z", "endTime": "2026-01-01T01:00:00Z"}}
        ],
    }
}


def _handler(connection: MagicMock | None) -> GoogleWebhookHandler:
    handler = GoogleWebhookHandler(data_247=MagicMock(), workouts=MagicMock())
    handler.connection_repo = MagicMock()
    handler.connection_repo.get_by_provider_user_id.return_value = connection
    return handler


def test_a_failed_notification_is_attributed_to_its_user() -> None:
    connection = MagicMock(user_id="ow-user-1")
    handler = _handler(connection)

    with (
        patch.object(handler, "_fetch_and_save", side_effect=RuntimeError("upstream 500")),
        patch("app.services.providers.google_health.webhook_handler.log_and_capture_error"),
    ):
        result = handler.process_payload(MagicMock(), [NOTIFICATION], "trace-1")

    item = result["results"][0]
    assert item["status"] == "error"
    assert item["error"] == "upstream 500"
    assert item["user_id"] == "ow-user-1"
    assert item["data_type"] == "steps"


def test_a_failure_before_the_connection_lookup_carries_no_user() -> None:
    handler = _handler(None)
    handler.connection_repo.get_by_provider_user_id.side_effect = RuntimeError("db down")

    with patch("app.services.providers.google_health.webhook_handler.log_and_capture_error"):
        result = handler.process_payload(MagicMock(), [NOTIFICATION], "trace-1")

    item = result["results"][0]
    assert item["status"] == "error"
    assert "user_id" not in item


def test_a_processed_notification_reports_user_and_count() -> None:
    connection = MagicMock(user_id="ow-user-1")
    handler = _handler(connection)

    with patch.object(handler, "_fetch_and_save", return_value=4):
        result = handler.process_payload(MagicMock(), [NOTIFICATION], "trace-1")

    item = result["results"][0]
    assert item == {
        "status": "processed",
        "data_type": "steps",
        "records_saved": 4,
        "user_id": "ow-user-1",
    }
    assert result["records_saved"] == 4
