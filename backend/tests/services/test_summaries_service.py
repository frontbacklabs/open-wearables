"""Tests for SummariesService."""

from datetime import date, datetime, timedelta, timezone
from logging import getLogger
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from app.schemas.enums import DeviceType, ProviderName
from app.services.priority_service import priority_service
from app.services.summaries_service import SummariesService
from tests.factories import (
    DataPointSeriesFactory,
    DataSourceFactory,
    EventRecordFactory,
    PersonalRecordFactory,
    SeriesTypeDefinitionFactory,
    SleepDetailsFactory,
    UserFactory,
    fake_firebase_uid,
)


@pytest.fixture
def service() -> SummariesService:
    return SummariesService(log=getLogger(__name__))


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


# ---------------------------------------------------------------------------
# _filter_by_priority
# ---------------------------------------------------------------------------


class TestFilterByPriority:
    def test_returns_empty_for_empty_input(self, db: Session, service: SummariesService) -> None:
        result = service._filter_by_priority(db, fake_firebase_uid(), [])
        assert result == []

    def test_single_entry_passes_through(self, db: Session, service: SummariesService) -> None:
        entry = {"activity_date": date(2026, 1, 1), "source": "garmin", "device_model": None}
        result = service._filter_by_priority(db, fake_firebase_uid(), [entry])
        assert result == [entry]

    def test_picks_one_entry_per_date(self, db: Session, service: SummariesService) -> None:
        entries = [
            {"activity_date": date(2026, 1, 1), "source": "garmin", "device_model": None},
            {"activity_date": date(2026, 1, 1), "source": "apple_health_sdk", "device_model": None},
            {"activity_date": date(2026, 1, 2), "source": "garmin", "device_model": None},
        ]
        result = service._filter_by_priority(db, fake_firebase_uid(), entries)
        assert len(result) == 2
        dates = {r["activity_date"] for r in result}
        assert dates == {date(2026, 1, 1), date(2026, 1, 2)}

    def test_uses_sleep_date_key(self, db: Session, service: SummariesService) -> None:
        entries = [
            {"sleep_date": date(2026, 1, 1), "source": "garmin", "device_model": None},
            {"sleep_date": date(2026, 1, 1), "source": "oura", "device_model": None},
        ]
        result = service._filter_by_priority(db, fake_firebase_uid(), entries, date_key="sleep_date")
        assert len(result) == 1


# ---------------------------------------------------------------------------
# _get_user_max_hr
# ---------------------------------------------------------------------------


class TestGetUserMaxHr:
    def test_falls_back_to_default_when_no_user(self, db: Session, service: SummariesService) -> None:
        result = service._get_user_max_hr(db, fake_firebase_uid(), datetime(2026, 1, 1, tzinfo=timezone.utc))
        assert result == 190

    def test_falls_back_to_default_when_no_birth_date(self, db: Session, service: SummariesService) -> None:
        user = UserFactory()
        PersonalRecordFactory(user=user, birth_date=None)
        result = service._get_user_max_hr(db, user.id, datetime(2026, 1, 1, tzinfo=timezone.utc))
        assert result == 190

    def test_calculates_from_birth_date(self, db: Session, service: SummariesService) -> None:
        user = UserFactory()
        PersonalRecordFactory(user=user, birth_date=date(1990, 6, 1))
        ref = datetime(2026, 6, 26, tzinfo=timezone.utc)  # age = 36
        result = service._get_user_max_hr(db, user.id, ref)
        assert result == 220 - 36

    def test_adjusts_when_birthday_not_yet_this_year(self, db: Session, service: SummariesService) -> None:
        user = UserFactory()
        PersonalRecordFactory(user=user, birth_date=date(1990, 12, 31))
        ref = datetime(2026, 6, 26, tzinfo=timezone.utc)  # birthday hasn't happened yet -> age 35
        result = service._get_user_max_hr(db, user.id, ref)
        assert result == 220 - 35


# ---------------------------------------------------------------------------
# get_sleep_summaries
# ---------------------------------------------------------------------------


class TestGetSleepSummaries:
    def _make_sleep_record(self, user: Any, start: str, end: str) -> Any:
        ds = DataSourceFactory(user=user, provider=ProviderName.GARMIN, source="garmin")
        return EventRecordFactory(
            data_source=ds,
            category="sleep",
            type="sleep",
            start_datetime=_dt(start),
            end_datetime=_dt(end),
            duration_seconds=int((_dt(end) - _dt(start)).total_seconds()),
            zone_offset="+00:00",
        )

    def _make_sleep_for_date(self, data_source: Any, sleep_date: date, record_id: UUID | None = None) -> Any:
        end = datetime(sleep_date.year, sleep_date.month, sleep_date.day, 6, tzinfo=timezone.utc)
        values = {
            "data_source": data_source,
            "category": "sleep",
            "type": "sleep",
            "start_datetime": end - timedelta(hours=8),
            "end_datetime": end,
            "duration_seconds": 8 * 3600,
            "zone_offset": "+00:00",
        }
        if record_id is not None:
            values["id"] = record_id
        return EventRecordFactory(**values)

    def test_returns_empty_when_no_data(self, db: Session, service: SummariesService) -> None:
        user = UserFactory()
        result = service.get_sleep_summaries(
            db,
            user.id,
            _dt("2026-01-01T00:00:00+00:00"),
            _dt("2026-01-07T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )
        assert result.data == []
        assert result.pagination.has_more is False

    def test_returns_sleep_record(self, db: Session, service: SummariesService) -> None:
        user = UserFactory()
        record = self._make_sleep_record(user, "2026-01-01T23:00:00+00:00", "2026-01-02T07:00:00+00:00")
        SleepDetailsFactory(event_record=record)

        result = service.get_sleep_summaries(
            db,
            user.id,
            _dt("2026-01-01T00:00:00+00:00"),
            _dt("2026-01-03T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )
        assert len(result.data) == 1
        summary = result.data[0]
        assert summary.duration_minutes == 8 * 60
        assert summary.source.provider == "garmin"

    def test_does_not_return_other_users_data(self, db: Session, service: SummariesService) -> None:
        user_a = UserFactory()
        user_b = UserFactory()
        self._make_sleep_record(user_b, "2026-01-01T23:00:00+00:00", "2026-01-02T07:00:00+00:00")

        result = service.get_sleep_summaries(
            db,
            user_a.id,
            _dt("2026-01-01T00:00:00+00:00"),
            _dt("2026-01-03T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )
        assert result.data == []

    def test_physio_averages_within_sleep_window(self, db: Session, service: SummariesService) -> None:
        """avg_heart_rate_bpm/avg_hrv_sdnn_ms are computed from data_point_series
        samples within [min_start_time, max_end_time), independently per series
        type, and samples outside the window are excluded."""
        user = UserFactory()
        ds = DataSourceFactory(user=user, provider=ProviderName.GARMIN, source="garmin")
        record = EventRecordFactory(
            data_source=ds,
            category="sleep",
            type="sleep",
            start_datetime=_dt("2026-01-01T23:00:00+00:00"),
            end_datetime=_dt("2026-01-02T07:00:00+00:00"),
            duration_seconds=8 * 3600,
            zone_offset="+00:00",
        )
        SleepDetailsFactory(event_record=record)

        hr_type = SeriesTypeDefinitionFactory.get_or_create_heart_rate()
        hrv_type = SeriesTypeDefinitionFactory.get_or_create_heart_rate_variability_sdnn()

        for i, val in enumerate([50, 60, 70]):
            DataPointSeriesFactory(
                data_source=ds,
                series_type=hr_type,
                value=val,
                recorded_at=_dt(f"2026-01-02T0{i}:00:00+00:00"),
            )
        DataPointSeriesFactory(
            data_source=ds,
            series_type=hrv_type,
            value=45,
            recorded_at=_dt("2026-01-02T02:00:00+00:00"),
        )
        # Outside the sleep window - must not affect the average
        DataPointSeriesFactory(
            data_source=ds,
            series_type=hr_type,
            value=200,
            recorded_at=_dt("2026-01-02T12:00:00+00:00"),
        )

        result = service.get_sleep_summaries(
            db,
            user.id,
            _dt("2026-01-01T00:00:00+00:00"),
            _dt("2026-01-03T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )
        assert len(result.data) == 1
        summary = result.data[0]
        assert summary.avg_heart_rate_bpm == 60
        assert summary.avg_hrv_sdnn_ms == 45

    def test_physio_averages_none_without_physio_data(self, db: Session, service: SummariesService) -> None:
        user = UserFactory()
        record = self._make_sleep_record(user, "2026-01-01T23:00:00+00:00", "2026-01-02T07:00:00+00:00")
        SleepDetailsFactory(event_record=record)

        result = service.get_sleep_summaries(
            db,
            user.id,
            _dt("2026-01-01T00:00:00+00:00"),
            _dt("2026-01-03T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )
        assert len(result.data) == 1
        summary = result.data[0]
        assert summary.avg_heart_rate_bpm is None
        assert summary.avg_hrv_sdnn_ms is None
        assert summary.avg_hrv_rmssd_ms is None
        assert summary.avg_respiratory_rate is None
        assert summary.avg_spo2_percent is None

    def test_has_more_flag_and_pagination(self, db: Session, service: SummariesService) -> None:
        user = UserFactory()
        ds = DataSourceFactory(user=user, provider=ProviderName.GARMIN, source="garmin")
        for day in range(1, 6):
            EventRecordFactory(
                data_source=ds,
                category="sleep",
                type="sleep",
                start_datetime=_dt(f"2026-01-{day:02d}T23:00:00+00:00"),
                end_datetime=_dt(f"2026-01-{day + 1:02d}T07:00:00+00:00"),
                duration_seconds=8 * 3600,
                zone_offset="+00:00",
            )

        result = service.get_sleep_summaries(
            db,
            user.id,
            _dt("2026-01-01T00:00:00+00:00"),
            _dt("2026-01-10T00:00:00+00:00"),
            cursor=None,
            limit=3,
        )
        assert [summary.date for summary in result.data] == [date(2026, 1, 2), date(2026, 1, 3), date(2026, 1, 4)]
        assert result.pagination.has_more is True
        assert result.pagination.next_cursor is not None

        second_page = service.get_sleep_summaries(
            db,
            user.id,
            _dt("2026-01-01T00:00:00+00:00"),
            _dt("2026-01-10T00:00:00+00:00"),
            cursor=result.pagination.next_cursor,
            limit=3,
        )
        assert [summary.date for summary in second_page.data] == [date(2026, 1, 5), date(2026, 1, 6)]
        assert second_page.pagination.has_more is False
        assert second_page.pagination.next_cursor is None

    def test_multi_provider_pagination_uses_unique_sleep_dates(self, db: Session, service: SummariesService) -> None:
        user = UserFactory()
        oura = DataSourceFactory(
            user=user,
            provider=ProviderName.OURA,
            source="oura",
            device_model="Oura Ring",
            device_type="ring",
        )
        google = DataSourceFactory(
            user=user,
            provider=ProviderName.GOOGLE,
            source="google_health",
            device_model="Pixel Watch",
            device_type="watch",
        )
        priority_service.update_provider_priority(db, ProviderName.OURA, 1)
        priority_service.update_provider_priority(db, ProviderName.GOOGLE, 2)
        first_date = datetime(2025, 1, 1, tzinfo=timezone.utc)

        for day_offset in range(120):
            sleep_date = first_date + timedelta(days=day_offset)
            for data_source in (oura, google):
                EventRecordFactory(
                    data_source=data_source,
                    category="sleep",
                    type="sleep",
                    start_datetime=sleep_date - timedelta(hours=2),
                    end_datetime=sleep_date + timedelta(hours=6),
                    duration_seconds=8 * 3600,
                    zone_offset="+00:00",
                )

        end_date = first_date + timedelta(days=120)
        first_page = service.get_sleep_summaries(
            db,
            user.id,
            first_date,
            end_date,
            cursor=None,
            limit=100,
        )

        assert len(first_page.data) == 100
        assert first_page.pagination.has_more is True
        assert first_page.pagination.next_cursor is not None

        second_page = service.get_sleep_summaries(
            db,
            user.id,
            first_date,
            end_date,
            cursor=first_page.pagination.next_cursor,
            limit=100,
        )

        assert len(second_page.data) == 20
        assert second_page.pagination.has_more is False
        dates = [summary.date for summary in first_page.data + second_page.data]
        assert dates == [(first_date + timedelta(days=offset)).date() for offset in range(120)]
        assert all(summary.source.provider == ProviderName.OURA for summary in first_page.data + second_page.data)

    def test_priority_selection_is_per_date_with_lower_priority_fallback(
        self, db: Session, service: SummariesService
    ) -> None:
        user = UserFactory()
        oura = DataSourceFactory(
            user=user,
            provider=ProviderName.OURA,
            source="oura",
            device_model="Oura Ring",
            device_type="ring",
        )
        google = DataSourceFactory(
            user=user,
            provider=ProviderName.GOOGLE,
            source="google_health",
            device_model="Pixel Watch",
            device_type="watch",
        )
        priority_service.update_provider_priority(db, ProviderName.OURA, 1)
        priority_service.update_provider_priority(db, ProviderName.GOOGLE, 2)
        self._make_sleep_for_date(oura, date(2025, 1, 1))
        self._make_sleep_for_date(google, date(2025, 1, 1))
        self._make_sleep_for_date(google, date(2025, 1, 2))

        result = service.get_sleep_summaries(
            db,
            user.id,
            _dt("2025-01-01T00:00:00+00:00"),
            _dt("2025-01-03T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )

        assert [summary.date for summary in result.data] == [date(2025, 1, 1), date(2025, 1, 2)]
        assert [summary.source.provider for summary in result.data] == [ProviderName.OURA, ProviderName.GOOGLE]

    def test_device_type_priority_precedes_device_model(self, db: Session, service: SummariesService) -> None:
        user = UserFactory()
        watch = DataSourceFactory(
            user=user,
            provider=ProviderName.OURA,
            source="oura_watch",
            device_model="A Watch",
            device_type="ring",
        )
        later_ring = DataSourceFactory(
            user=user,
            provider=ProviderName.OURA,
            source="oura_ring_z",
            device_model="Z Ring",
            device_type="watch",
        )
        earlier_ring = DataSourceFactory(
            user=user,
            provider=ProviderName.OURA,
            source="oura_ring_y",
            device_model="Y Ring",
            device_type="watch",
        )
        priority_service.update_provider_priority(db, ProviderName.OURA, 1)
        priority_service.update_device_type_priority(db, DeviceType.RING, 1)
        priority_service.update_device_type_priority(db, DeviceType.WATCH, 2)
        for data_source in (watch, later_ring, earlier_ring):
            self._make_sleep_for_date(data_source, date(2025, 1, 1))

        result = service.get_sleep_summaries(
            db,
            user.id,
            _dt("2025-01-01T00:00:00+00:00"),
            _dt("2025-01-02T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )

        assert len(result.data) == 1
        assert result.data[0].source.device == "Y Ring"

    def test_exact_priority_ties_are_stable_across_forward_and_backward_pages(
        self, db: Session, service: SummariesService
    ) -> None:
        user = UserFactory()
        oura = DataSourceFactory(
            user=user,
            provider=ProviderName.OURA,
            source="oura",
            device_model="Shared Ring",
            device_type="ring",
        )
        google = DataSourceFactory(
            user=user,
            provider=ProviderName.GOOGLE,
            source="google_health",
            device_model="Shared Ring",
            device_type="ring",
        )
        priority_service.update_provider_priority(db, ProviderName.OURA, 1)
        priority_service.update_provider_priority(db, ProviderName.GOOGLE, 1)
        priority_service.update_device_type_priority(db, DeviceType.RING, 1)
        expected_dates = [date(2025, 1, day) for day in range(1, 6)]
        for index, sleep_date in enumerate(expected_dates):
            self._make_sleep_for_date(oura, sleep_date, UUID(int=100 + index * 2))
            self._make_sleep_for_date(google, sleep_date, UUID(int=101 + index * 2))

        first_page = service.get_sleep_summaries(
            db,
            user.id,
            _dt("2025-01-01T00:00:00+00:00"),
            _dt("2025-01-06T00:00:00+00:00"),
            cursor=None,
            limit=2,
        )
        second_page = service.get_sleep_summaries(
            db,
            user.id,
            _dt("2025-01-01T00:00:00+00:00"),
            _dt("2025-01-06T00:00:00+00:00"),
            cursor=first_page.pagination.next_cursor,
            limit=2,
        )
        third_page = service.get_sleep_summaries(
            db,
            user.id,
            _dt("2025-01-01T00:00:00+00:00"),
            _dt("2025-01-06T00:00:00+00:00"),
            cursor=second_page.pagination.next_cursor,
            limit=2,
        )

        forward_dates = [summary.date for page in (first_page, second_page, third_page) for summary in page.data]
        assert forward_dates == expected_dates
        assert all(
            summary.source.provider == ProviderName.OURA
            for page in (first_page, second_page, third_page)
            for summary in page.data
        )

        repeated_first_page = service.get_sleep_summaries(
            db,
            user.id,
            _dt("2025-01-01T00:00:00+00:00"),
            _dt("2025-01-06T00:00:00+00:00"),
            cursor=None,
            limit=2,
        )
        assert [summary.model_dump() for summary in repeated_first_page.data] == [
            summary.model_dump() for summary in first_page.data
        ]

        backward_second_page = service.get_sleep_summaries(
            db,
            user.id,
            _dt("2025-01-01T00:00:00+00:00"),
            _dt("2025-01-06T00:00:00+00:00"),
            cursor=third_page.pagination.previous_cursor,
            limit=2,
        )
        assert [summary.model_dump() for summary in backward_second_page.data] == [
            summary.model_dump() for summary in second_page.data
        ]
        assert backward_second_page.pagination.has_more is True
        assert backward_second_page.pagination.previous_cursor is not None
        assert backward_second_page.pagination.next_cursor is not None

        backward_first_page = service.get_sleep_summaries(
            db,
            user.id,
            _dt("2025-01-01T00:00:00+00:00"),
            _dt("2025-01-06T00:00:00+00:00"),
            cursor=backward_second_page.pagination.previous_cursor,
            limit=2,
        )
        assert [summary.model_dump() for summary in backward_first_page.data] == [
            summary.model_dump() for summary in first_page.data
        ]
        assert backward_first_page.pagination.has_more is False
        assert backward_first_page.pagination.previous_cursor is None
        assert backward_first_page.pagination.next_cursor is not None

        forward_second_page = service.get_sleep_summaries(
            db,
            user.id,
            _dt("2025-01-01T00:00:00+00:00"),
            _dt("2025-01-06T00:00:00+00:00"),
            cursor=backward_first_page.pagination.next_cursor,
            limit=2,
        )
        assert [summary.model_dump() for summary in forward_second_page.data] == [
            summary.model_dump() for summary in second_page.data
        ]

    def test_sleep_summary_ranking_preserves_date_bounds_and_user_scope(
        self, db: Session, service: SummariesService
    ) -> None:
        user = UserFactory()
        other_user = UserFactory()
        user_source = DataSourceFactory(user=user, provider=ProviderName.OURA, source="oura")
        other_source = DataSourceFactory(user=other_user, provider=ProviderName.OURA, source="oura")
        for day in range(1, 5):
            self._make_sleep_for_date(user_source, date(2025, 1, day))
        self._make_sleep_for_date(other_source, date(2025, 1, 2))

        result = service.get_sleep_summaries(
            db,
            user.id,
            _dt("2025-01-02T00:00:00+00:00"),
            _dt("2025-01-04T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )

        assert [summary.date for summary in result.data] == [date(2025, 1, 2), date(2025, 1, 3)]


# ---------------------------------------------------------------------------
# get_recovery_summaries
# ---------------------------------------------------------------------------


class TestGetRecoverySummaries:
    def test_rmssd_input_is_exposed_as_rmssd_not_sdnn(
        self, service: SummariesService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        row = {
            "recovery_date": date(2026, 1, 2),
            "provider": "whoop",
            "source": "whoop",
            "device_model": "WHOOP",
            "device_type": "band",
            "record_id": fake_firebase_uid(),
            "recorded_at": _dt("2026-01-02T00:00:00+00:00"),
            "recovery_score": 74,
            "resting_heart_rate": 51,
            "hrv_rmssd_milli": 63.2,
            "spo2_percentage": 98.4,
        }
        monkeypatch.setattr(service.health_score_repo, "get_recovery_summaries", lambda *_: [row])
        monkeypatch.setattr(service, "_filter_by_priority", lambda *_args, **_kwargs: [row])

        result = service.get_recovery_summaries(
            db_session=None,
            user_id=fake_firebase_uid(),
            start_date=_dt("2026-01-01T00:00:00+00:00"),
            end_date=_dt("2026-01-03T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )

        summary = result.data[0]
        assert summary.avg_hrv_sdnn_ms is None
        assert summary.avg_hrv_rmssd_ms == 63.2


# ---------------------------------------------------------------------------
# get_activity_summaries
# ---------------------------------------------------------------------------


class TestGetActivitySummaries:
    def test_returns_empty_when_no_data(self, db: Session, service: SummariesService) -> None:
        user = UserFactory()
        result = service.get_activity_summaries(
            db,
            user.id,
            _dt("2026-01-01T00:00:00+00:00"),
            _dt("2026-01-07T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )
        assert result.data == []

    def test_aggregates_steps_for_user(self, db: Session, service: SummariesService) -> None:
        user = UserFactory()
        ds = DataSourceFactory(user=user, provider=ProviderName.APPLE, source="apple_health_sdk")
        steps_type = SeriesTypeDefinitionFactory.get_or_create_steps()

        for i in range(3):
            DataPointSeriesFactory(
                data_source=ds,
                series_type=steps_type,
                value=1000,
                recorded_at=_dt(f"2026-01-01T10:0{i}:00+00:00"),
            )

        result = service.get_activity_summaries(
            db,
            user.id,
            _dt("2026-01-01T00:00:00+00:00"),
            _dt("2026-01-02T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )
        assert len(result.data) == 1
        assert result.data[0].steps == 3000

    def test_does_not_return_other_users_data(self, db: Session, service: SummariesService) -> None:
        user_a = UserFactory()
        user_b = UserFactory()
        ds = DataSourceFactory(user=user_b)
        steps_type = SeriesTypeDefinitionFactory.get_or_create_steps()
        DataPointSeriesFactory(
            data_source=ds, series_type=steps_type, value=5000, recorded_at=_dt("2026-01-01T10:00:00+00:00")
        )

        result = service.get_activity_summaries(
            db,
            user_a.id,
            _dt("2026-01-01T00:00:00+00:00"),
            _dt("2026-01-02T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )
        assert result.data == []
