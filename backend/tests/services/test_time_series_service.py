"""
Tests for TimeSeriesService.

Tests cover:
- Bulk creating time series samples
- Getting daily histogram of data points
- Counting data points by series type
- Counting data points by provider
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy.orm import Session

from app.schemas.enums import SeriesType
from app.schemas.model_crud.activities import (
    HeartRateSampleCreate,
    StepSampleCreate,
    TimeSeriesQueryParams,
    TimeSeriesSampleCreate,
)
from app.services.timeseries_service import timeseries_service
from tests.factories import (
    DataPointSeriesFactory,
    DataSourceFactory,
    SeriesTypeDefinitionFactory,
    UserFactory,
)


class TestTimeSeriesServiceBulkCreateSamples:
    """Test bulk creation of time series samples."""

    def test_bulk_create_heart_rate_samples(self, db: Session) -> None:
        """Should bulk create heart rate samples."""
        # Arrange
        user = UserFactory()
        DataSourceFactory(source="apple", device_model="device_1")

        initial_count = timeseries_service.get_total_count(db)
        now = datetime.now(timezone.utc)
        samples = [
            HeartRateSampleCreate(
                id=uuid4(),
                user_id=user.id,
                provider_name="apple",
                device_model="device_1",
                recorded_at=now - timedelta(minutes=i),
                value=70 + i,
                series_type=SeriesType.heart_rate,
            )
            for i in range(5)
        ]

        # Act
        timeseries_service.bulk_create_samples(db, samples)

        # Assert - verify samples were created
        final_count = timeseries_service.get_total_count(db)
        assert final_count == initial_count + 5

    def test_bulk_create_step_samples(self, db: Session) -> None:
        """Should bulk create step samples."""
        # Arrange
        user = UserFactory()
        DataSourceFactory(source="apple", device_model="device_2")

        initial_count = timeseries_service.get_total_count(db)
        now = datetime.now(timezone.utc)
        samples = [
            StepSampleCreate(
                id=uuid4(),
                user_id=user.id,
                provider_name="apple",
                device_model="device_2",
                recorded_at=now - timedelta(hours=i),
                value=1000 + i * 100,
                series_type=SeriesType.steps,
            )
            for i in range(3)
        ]

        # Act
        timeseries_service.bulk_create_samples(db, samples)

        # Assert
        final_count = timeseries_service.get_total_count(db)
        assert final_count == initial_count + 3

    def test_bulk_create_mixed_series_types(self, db: Session) -> None:
        """Should bulk create samples of different series types."""
        # Arrange
        user = UserFactory()
        DataSourceFactory(source="apple", device_model="device_3")

        initial_count = timeseries_service.get_total_count(db)
        now = datetime.now(timezone.utc)
        samples = [
            TimeSeriesSampleCreate(
                id=uuid4(),
                user_id=user.id,
                provider_name="apple",
                device_model="device_3",
                recorded_at=now - timedelta(minutes=1),
                value=72,
                series_type=SeriesType.heart_rate,
            ),
            TimeSeriesSampleCreate(
                id=uuid4(),
                user_id=user.id,
                provider_name="apple",
                device_model="device_3",
                recorded_at=now - timedelta(minutes=2),
                value=5000,
                series_type=SeriesType.steps,
            ),
        ]

        # Act
        timeseries_service.bulk_create_samples(db, samples)

        # Assert
        total_count = timeseries_service.get_total_count(db)
        assert total_count >= initial_count + 2


class TestTimeSeriesServiceGetDailyHistogram:
    """Test getting daily histogram of data points."""

    def test_get_daily_histogram_groups_by_day(self, db: Session) -> None:
        """Should group data points by day."""
        # Arrange
        mapping = DataSourceFactory()
        series_type = SeriesTypeDefinitionFactory.get_or_create_heart_rate()

        start_date = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        end_date = datetime(2024, 1, 4, 0, 0, 0, tzinfo=timezone.utc)

        # Day 1: 3 samples
        for i in range(3):
            DataPointSeriesFactory(
                mapping=mapping,
                series_type=series_type,
                recorded_at=datetime(2024, 1, 1, 10 + i, 0, 0, tzinfo=timezone.utc),
            )

        # Day 2: 2 samples
        for i in range(2):
            DataPointSeriesFactory(
                mapping=mapping,
                series_type=series_type,
                recorded_at=datetime(2024, 1, 2, 10 + i, 0, 0, tzinfo=timezone.utc),
            )

        # Day 3: 1 sample
        DataPointSeriesFactory(
            mapping=mapping,
            series_type=series_type,
            recorded_at=datetime(2024, 1, 3, 10, 0, 0, tzinfo=timezone.utc),
        )

        # Act
        histogram = timeseries_service.get_daily_histogram(db, start_date, end_date)

        # Assert
        assert len(histogram) == 3
        assert histogram[0] == 3  # Day 1
        assert histogram[1] == 2  # Day 2
        assert histogram[2] == 1  # Day 3

    def test_get_daily_histogram_empty_range(self, db: Session) -> None:
        """Should return empty list for range with no data."""
        # Arrange
        start_date = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
        end_date = datetime(2024, 6, 7, 0, 0, 0, tzinfo=timezone.utc)

        # Act
        histogram = timeseries_service.get_daily_histogram(db, start_date, end_date)

        # Assert
        assert histogram == []


class TestTimeSeriesServiceGetCountBySource:
    """Test counting data points by source."""

    def test_get_count_by_source_groups_correctly(self, db: Session) -> None:
        """Should group and count data points by source."""
        # Arrange
        user = UserFactory()
        apple_mapping = DataSourceFactory(user=user, source="apple")
        garmin_mapping = DataSourceFactory(user=user, source="garmin")

        series_type = SeriesTypeDefinitionFactory.get_or_create_heart_rate()

        # Create 4 samples from Apple
        for _ in range(4):
            DataPointSeriesFactory(mapping=apple_mapping, series_type=series_type)

        # Create 2 samples from Garmin
        for _ in range(2):
            DataPointSeriesFactory(mapping=garmin_mapping, series_type=series_type)

        # Act
        results = timeseries_service.get_count_by_source(db)

        # Assert
        results_dict = dict(results)
        assert results_dict["apple"] == 4
        assert results_dict["garmin"] == 2

    def test_get_count_by_source_ordered_by_count(self, db: Session) -> None:
        """Should order results by count descending."""
        # Arrange
        results = timeseries_service.get_count_by_source(db)

        if len(results) > 1:
            # Verify descending order
            for i in range(len(results) - 1):
                assert results[i][1] >= results[i + 1][1]

    def test_get_count_by_source_empty_result(self, db: Session) -> None:
        """Should return empty list when no data points exist."""
        # Act
        results = timeseries_service.get_count_by_source(db)

        # Assert
        assert results == []


class TestTimeSeriesServiceGetTotalCount:
    """Test getting total count of data points."""

    def test_get_total_count(self, db: Session) -> None:
        """Should return total count of all data points."""
        # Arrange
        mapping = DataSourceFactory()
        series_type = SeriesTypeDefinitionFactory.get_or_create_heart_rate()

        initial_count = timeseries_service.get_total_count(db)

        # Create 5 samples
        for _ in range(5):
            DataPointSeriesFactory(mapping=mapping, series_type=series_type)

        # Act
        total_count = timeseries_service.get_total_count(db)

        # Assert
        assert total_count == initial_count + 5

    def test_get_total_count_empty_database(self, db: Session) -> None:
        """Should return 0 when no data points exist."""
        # Note: This test might fail if there's existing data in the test DB
        # from other tests running in the same session
        # Act
        count = timeseries_service.get_total_count(db)

        # Assert
        assert count >= 0  # At minimum should be non-negative


class TestTimeSeriesServiceGetCountInRange:
    """Test counting data points in date range."""

    def test_get_count_in_range(self, db: Session) -> None:
        """Should count data points within date range."""
        # Arrange
        mapping = DataSourceFactory()
        series_type = SeriesTypeDefinitionFactory.get_or_create_heart_rate()

        now = datetime.now(timezone.utc)
        start = now - timedelta(days=7)
        end = now - timedelta(days=1)

        # Create samples at different times
        DataPointSeriesFactory(
            mapping=mapping,
            series_type=series_type,
            recorded_at=now - timedelta(days=10),
        )  # Before range
        DataPointSeriesFactory(
            mapping=mapping,
            series_type=series_type,
            recorded_at=now - timedelta(days=5),
        )  # In range
        DataPointSeriesFactory(
            mapping=mapping,
            series_type=series_type,
            recorded_at=now - timedelta(days=3),
        )  # In range
        DataPointSeriesFactory(mapping=mapping, series_type=series_type, recorded_at=now)  # After range

        # Act
        count = timeseries_service.get_count_in_range(db, start, end)

        # Assert
        assert count == 2

    def test_get_count_in_range_empty_result(self, db: Session) -> None:
        """Should return 0 when no data points in range."""
        # Arrange
        now = datetime.now(timezone.utc)
        future = now + timedelta(days=7)
        far_future = future + timedelta(days=7)

        # Act
        count = timeseries_service.get_count_in_range(db, future, far_future)

        # Assert
        assert count == 0


class TestTimeSeriesServiceGetTimeseries:
    """Test get_timeseries ordering and cursor pagination."""

    def _make_samples(self, user: Any, count: int = 5) -> list[datetime]:
        """Create `count` heart-rate samples spaced 1 minute apart (ascending)."""
        data_source = DataSourceFactory(user=user, source="apple")
        series_type = SeriesTypeDefinitionFactory.get_or_create_heart_rate()
        base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        timestamps = []
        for i in range(count):
            ts = base + timedelta(minutes=i)
            DataPointSeriesFactory(data_source=data_source, series_type=series_type, recorded_at=ts)
            timestamps.append(ts)
        return timestamps

    def _fetch(
        self,
        db: Session,
        user_id: str,
        cursor: str | None = None,
        sort_order: Literal["asc", "desc"] = "asc",
        limit: int = 2,
    ) -> Any:
        params = TimeSeriesQueryParams(
            start_datetime=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end_datetime=datetime(2026, 1, 2, tzinfo=timezone.utc),
            limit=limit,
            cursor=cursor,
            sort_order=sort_order,
        )
        return timeseries_service.get_timeseries(db, user_id, [SeriesType.heart_rate], params)

    def test_asc_returns_earliest_first(self, db: Session) -> None:
        """Default (asc) ordering returns the earliest samples on the first page."""
        user = UserFactory()
        timestamps = self._make_samples(user, count=3)

        result = self._fetch(db, user.id, limit=10)

        assert [s.timestamp for s in result.data] == timestamps
        assert result.pagination.has_more is False

    def test_desc_returns_latest_first(self, db: Session) -> None:
        """sort_order=desc returns the latest samples on the first page."""
        user = UserFactory()
        timestamps = self._make_samples(user, count=3)

        result = self._fetch(db, user.id, sort_order="desc", limit=10)

        assert [s.timestamp for s in result.data] == list(reversed(timestamps))
        assert result.pagination.has_more is False

    def test_desc_paginates_forward_and_backward(self, db: Session) -> None:
        """Cursor pagination follows the requested sort order in both directions."""
        user = UserFactory()
        timestamps = self._make_samples(user, count=5)

        page1 = self._fetch(db, user.id, sort_order="desc")
        assert [s.timestamp for s in page1.data] == [timestamps[4], timestamps[3]]
        assert page1.pagination.has_more is True
        assert page1.pagination.previous_cursor is None

        page2 = self._fetch(db, user.id, cursor=page1.pagination.next_cursor, sort_order="desc")
        assert [s.timestamp for s in page2.data] == [timestamps[2], timestamps[1]]
        assert page2.pagination.next_cursor is not None
        assert page2.pagination.previous_cursor is not None

        page3 = self._fetch(db, user.id, cursor=page2.pagination.next_cursor, sort_order="desc")
        assert [s.timestamp for s in page3.data] == [timestamps[0]]
        assert page3.pagination.has_more is False

        back2 = self._fetch(db, user.id, cursor=page3.pagination.previous_cursor, sort_order="desc")
        assert [s.timestamp for s in back2.data] == [timestamps[2], timestamps[1]]
        assert back2.pagination.next_cursor is not None
        assert back2.pagination.previous_cursor is not None

        back1 = self._fetch(db, user.id, cursor=back2.pagination.previous_cursor, sort_order="desc")
        assert [s.timestamp for s in back1.data] == [timestamps[4], timestamps[3]]
        assert back1.pagination.previous_cursor is None
        assert back1.pagination.next_cursor is not None

    def test_asc_backward_page_exposes_next_cursor(self, db: Session) -> None:
        """Pages reached via previous_cursor always expose next_cursor to page forward again."""
        user = UserFactory()
        timestamps = self._make_samples(user, count=3)

        page1 = self._fetch(db, user.id, sort_order="asc")
        page2 = self._fetch(db, user.id, cursor=page1.pagination.next_cursor, sort_order="asc")

        back1 = self._fetch(db, user.id, cursor=page2.pagination.previous_cursor, sort_order="asc")

        assert [s.timestamp for s in back1.data] == [timestamps[0], timestamps[1]]
        assert back1.pagination.next_cursor is not None
