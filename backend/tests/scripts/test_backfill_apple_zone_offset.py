"""Tests for the Apple zone offset backfill, see scripts/data_migrations/backfill_apple_zone_offset.py."""

import importlib.util
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.integrations.rollup_queue import pop_dirty_days
from app.models import ArchivalSetting, SeriesAggregate, SeriesSample
from app.repositories.archival_repository import ArchivalSettingRepository
from app.repositories.series_rollup_repository import SeriesRollupRepository
from app.schemas.enums.provider import ProviderName
from tests.factories import DataPointSeriesFactory, DataSourceFactory, SeriesTypeDefinitionFactory, UserFactory

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "data_migrations" / "backfill_apple_zone_offset.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("backfill_apple_zone_offset", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_module = _load_module()
backfill = _module.backfill_apple_zone_offset
read_zones = _module.read_zones

NEW_YORK = "America/New_York"
# 20:00 on Nov 16 in New York, already Nov 17 in UTC.
_EVENING = datetime(2026, 11, 17, 1, 0, tzinfo=timezone.utc)
_MORNING = datetime(2026, 11, 16, 14, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _rollup_on(db: Session) -> None:
    db.merge(ArchivalSetting(id=1, rollup_enabled=True, delete_after_days=None, deleted_before=None))
    db.flush()


def _source(provider: ProviderName = ProviderName.APPLE) -> Any:
    return DataSourceFactory(user=UserFactory(), provider=provider)


def _steps(source: Any, at: datetime, value: str = "100", **kwargs: Any) -> Any:
    return DataPointSeriesFactory(
        data_source=source,
        series_type=SeriesTypeDefinitionFactory.get_or_create_steps(),
        recorded_at=at,
        value=value,
        zone_offset=kwargs.pop("zone_offset", None),
        **kwargs,
    )


def _offset(db: Session, sample: Any) -> str | None:
    db.expire_all()
    row = db.get(SeriesSample, sample.id)
    assert row is not None
    return row.zone_offset


class TestOffsets:
    def test_a_sample_without_an_offset_gets_its_users_zone(self, db: Session) -> None:
        # Arrange
        source = _source()
        sample = _steps(source, _EVENING)

        # Act
        counts = backfill(db, {source.user_id: NEW_YORK}, dry_run=False)

        # Assert
        assert _offset(db, sample) == "-05:00"
        assert (counts["samples"], counts["days_moved"]) == (1, 1)

    def test_daylight_time_follows_each_samples_own_date(self, db: Session) -> None:
        # Arrange
        source = _source()
        summer = _steps(source, datetime(2026, 7, 1, 23, 30, tzinfo=timezone.utc))
        winter = _steps(source, _EVENING)

        # Act
        backfill(db, {source.user_id: NEW_YORK}, dry_run=False)

        # Assert
        assert (_offset(db, summer), _offset(db, winter)) == ("-04:00", "-05:00")

    def test_half_hour_and_east_of_utc_zones_keep_their_minutes_and_sign(self, db: Session) -> None:
        # Arrange
        source = _source()
        sample = _steps(source, _EVENING)

        # Act
        backfill(db, {source.user_id: "Asia/Kolkata"}, dry_run=False)

        # Assert
        assert _offset(db, sample) == "+05:30"

    def test_leaves_offsets_daily_totals_other_providers_and_other_users_alone(self, db: Session) -> None:
        # Arrange
        source = _source()
        with_offset = _steps(source, _EVENING, zone_offset="+09:00")
        daily_total = _steps(source, datetime(2026, 11, 16, tzinfo=timezone.utc), "9000", is_daily_total=True)
        oura = _steps(_source(ProviderName.OURA), _EVENING)
        other_user = _steps(_source(), _EVENING)

        # Act
        backfill(db, {source.user_id: NEW_YORK}, dry_run=False)

        # Assert
        assert _offset(db, with_offset) == "+09:00"
        assert [_offset(db, s) for s in (daily_total, oura, other_user)] == [None, None, None]

    def test_dry_run_changes_nothing_and_queues_nothing(self, db: Session) -> None:
        # Arrange
        pop_dirty_days(100)
        source = _source()
        sample = _steps(source, _EVENING)

        # Act
        counts = backfill(db, {source.user_id: NEW_YORK}, dry_run=True)

        # Assert
        assert counts["samples"] == 1
        assert _offset(db, sample) is None
        assert pop_dirty_days(100) == []

    def test_an_unknown_zone_skips_its_user(self, db: Session) -> None:
        # Arrange
        source = _source()
        sample = _steps(source, _EVENING)

        # Act
        counts = backfill(db, {source.user_id: "Mars/Olympus_Mons"}, dry_run=False)

        # Assert
        assert _offset(db, sample) is None
        assert counts["unknown_zone"] == 1

    def test_samples_weeks_apart_are_all_changed(self, db: Session) -> None:
        # Arrange
        source = _source()
        samples = [_steps(source, datetime(2026, month, 3, 12, tzinfo=timezone.utc)) for month in (1, 2, 6, 11)]

        # Act
        backfill(db, {source.user_id: NEW_YORK}, dry_run=False)

        # Assert
        assert [_offset(db, s) for s in samples] == ["-05:00", "-05:00", "-04:00", "-05:00"]


class TestRollup:
    def test_queues_the_old_and_new_local_days(self, db: Session) -> None:
        # Arrange
        pop_dirty_days(100)
        source = _source()
        _steps(source, _EVENING)

        # Act
        backfill(db, {source.user_id: NEW_YORK}, dry_run=False)

        # Assert
        assert set(pop_dirty_days(100)) == {(source.id, date(2026, 11, 16)), (source.id, date(2026, 11, 17))}

    def test_the_evening_moves_onto_its_local_day_in_the_same_run(self, db: Session) -> None:
        # Arrange: rolled up while the samples read as UTC, the evening sits on Nov 17.
        source = _source()
        _steps(source, _MORNING, "1900")
        _steps(source, _EVENING, "155")
        SeriesRollupRepository().roll_up(db, source.id, [date(2026, 11, 16), date(2026, 11, 17)])

        # Act
        backfill(db, {source.user_id: NEW_YORK}, dry_run=False)

        # Assert
        db.expire_all()
        rows = db.query(SeriesAggregate).filter(SeriesAggregate.data_source_id == source.id).all()
        assert [(row.period_start, row.zone_offset, row.sum) for row in rows] == [
            (datetime(2026, 11, 16, 5, tzinfo=timezone.utc), "-05:00", Decimal("2055"))
        ]

    def test_with_the_rollup_off_the_days_rows_are_deleted_for_reads_to_count_the_samples(self, db: Session) -> None:
        # Arrange
        source = _source()
        _steps(source, _MORNING, "1900")
        _steps(source, _EVENING, "155")
        SeriesRollupRepository().roll_up(db, source.id, [date(2026, 11, 16), date(2026, 11, 17)])
        ArchivalSettingRepository().get(db).rollup_enabled = False
        db.flush()

        # Act
        backfill(db, {source.user_id: NEW_YORK}, dry_run=False)

        # Assert
        assert db.query(SeriesAggregate).filter(SeriesAggregate.data_source_id == source.id).count() == 0

    def test_days_retention_has_reached_keep_their_samples_and_rows(self, db: Session) -> None:
        # Arrange
        source = _source()
        old = _steps(source, datetime(2026, 11, 2, 1, tzinfo=timezone.utc))
        edge = _steps(source, datetime(2026, 11, 10, 1, tzinfo=timezone.utc))
        recent = _steps(source, _EVENING)
        SeriesRollupRepository().roll_up(db, source.id, [date(2026, 11, 2)])
        ArchivalSettingRepository().get(db).deleted_before = date(2026, 11, 10)
        db.flush()

        # Act
        backfill(db, {source.user_id: NEW_YORK}, dry_run=False)

        # Assert
        assert [_offset(db, s) for s in (old, edge, recent)] == [None, None, "-05:00"]
        rows = db.query(SeriesAggregate).filter(SeriesAggregate.data_source_id == source.id).all()
        assert (datetime(2026, 11, 2, tzinfo=timezone.utc), "+00:00") in [(r.period_start, r.zone_offset) for r in rows]


class TestReadZones:
    def test_reads_pairs_and_a_csv_with_a_header(self, tmp_path: Path) -> None:
        # Arrange
        zones_file = tmp_path / "zones.csv"
        zones_file.write_text("user_id,timezone\nabc,Asia/Tokyo\nno-zone,\n")

        # Act
        zones = read_zones(["xyz=America/New_York"], zones_file)

        # Assert
        assert zones == {"xyz": NEW_YORK, "abc": "Asia/Tokyo"}
