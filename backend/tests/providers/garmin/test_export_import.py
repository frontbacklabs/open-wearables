"""Tests for the Garmin account-export import adapter."""

import io
import json
import zipfile
from unittest.mock import MagicMock

from app.services.providers.garmin.export_import import (
    iter_export_records,
    run_export_import,
    summary_type_for,
)


def _zip(members: dict[str, object]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, payload in members.items():
            if isinstance(payload, bytes):
                zf.writestr(name, payload)
            else:
                zf.writestr(name, json.dumps(payload))
    return buf.getvalue()


class TestSummaryTypeFor:
    def test_wellness_files(self) -> None:
        assert summary_type_for("DI_CONNECT/DI-Connect-Wellness/2023-01-01_2023-02-01_123_sleepData.json") == "sleeps"
        assert summary_type_for("DI_CONNECT/DI-Connect-Wellness/2023-01-01_123_UDSFile.json") == "dailies"
        assert summary_type_for("DI_CONNECT/DI-Connect-Wellness/x_hrvData.json") == "hrv"
        assert summary_type_for("DI_CONNECT/DI-Connect-Wellness/x_stressData.json") == "stressDetails"
        assert summary_type_for("DI_CONNECT/DI-Connect-Wellness/x_bodyComposition.json") == "bodyComps"
        assert summary_type_for("DI_CONNECT/DI-Connect-Fitness/summarizedActivities.json") == "activities"

    def test_specific_beats_generic(self) -> None:
        # "moveIQActivities" and "summarizedActivities" contain "activities"/"sleep"-like
        # needles; ordering must route them to their own types.
        assert summary_type_for("x_moveIQActivities.json") == "moveIQActivities"

    def test_non_json_and_unknown(self) -> None:
        assert summary_type_for("DI_CONNECT/userProfile.json") is None
        assert summary_type_for("DI_CONNECT/DI-Connect-Fitness/1234.fit") is None
        assert summary_type_for("readme.txt") is None


class TestIterExportRecords:
    def test_collects_records_per_type(self) -> None:
        blob = _zip(
            {
                "DI_CONNECT/DI-Connect-Wellness/a_sleepData.json": [
                    {"calendarDate": "2024-01-01"},
                    {"calendarDate": "2024-01-02"},
                ],
                "DI_CONNECT/DI-Connect-Wellness/b_UDSFile.json": [{"calendarDate": "2024-01-01"}],
                "DI_CONNECT/userProfile.json": {"displayName": "x"},
            }
        )
        seen = [(t, r) for _m, t, r in iter_export_records(blob)]
        assert len(seen) == 3
        assert [t for t, _r in seen].count("sleeps") == 2
        assert [t for t, _r in seen].count("dailies") == 1

    def test_nested_zip_members(self) -> None:
        inner = _zip({"wellness/c_hrvData.json": [{"calendarDate": "2024-01-01"}]})
        outer = _zip({"DI_CONNECT/DI-Connect-Uploaded-Files/nested.zip": inner})
        seen = list(iter_export_records(outer))
        assert [(t) for _m, t, _r in seen] == ["hrv"]

    def test_dict_wrapped_list_payload(self) -> None:
        blob = _zip({"w/d_sleepData.json": {"sleepSummaries": [{"calendarDate": "2024-01-01"}]}})
        assert [r["calendarDate"] for _m, _t, r in iter_export_records(blob)] == ["2024-01-01"]


class TestRunExportImport:
    def test_groups_and_delegates_per_type(self) -> None:
        blob = _zip(
            {
                "w/s_sleepData.json": [{"calendarDate": "2024-01-01"}],
                "w/d_UDSFile.json": [{"calendarDate": "2024-01-01"}, {"calendarDate": "2024-01-02"}],
            }
        )
        data_247 = MagicMock()
        data_247.process_items_batch.side_effect = lambda _db, _uid, t, items: len(items)
        db = MagicMock()

        result = run_export_import(db, "user-1", [(blob, "export.zip")], data_247, "trace")

        assert result["items"] == {"sleeps": 1, "dailies": 2}
        assert result["saved"] == {"sleeps": 1, "dailies": 2}
        assert result["errors"] == []
        calls = {c.args[2]: c.args[3] for c in data_247.process_items_batch.call_args_list}
        assert calls["sleeps"] == [{"calendarDate": "2024-01-01"}]
        assert len(calls["dailies"]) == 2

    def test_type_failure_is_collected_not_raised(self) -> None:
        blob = _zip({"w/s_sleepData.json": [{"calendarDate": "2024-01-01"}]})
        data_247 = MagicMock()
        data_247.process_items_batch.side_effect = ValueError("boom")
        result = run_export_import(MagicMock(), "user-1", [(blob, "export.zip")], data_247, "trace")
        assert "sleeps" in result["errors"][0]
