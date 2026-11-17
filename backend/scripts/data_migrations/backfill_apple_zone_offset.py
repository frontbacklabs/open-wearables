#!/usr/bin/env python3
"""Give Apple SDK samples stored without a zone offset the offset of their user's time zone.

Before SDK ``0.17.0+fbl.2`` the iOS SDK sent ``zoneOffset: null`` for every HealthKit sample without
``HKMetadataKeyTimeZone``, which is nearly all of them (iPhone and Watch steps, heart rate, energy). A
missing offset reads as UTC (``sample_day_sql``), so for a user west of UTC every evening's samples were
rolled up into the next day, and each day's row ran from the previous local evening to the local evening.

Open Wearables doesn't know a user's time zone, so it is passed in: ``--zone USER_ID=ZONE`` (repeatable)
or ``--zones-file``, a CSV of ``user_id,timezone`` rows (a header row is skipped), e.g. Ren's
``user_data.users``. Each of the user's ``apple`` samples without an offset gets the zone's offset at its
own instant, so daylight saving time is applied per sample: the fallback the SDK now applies on the
device. Like the SDK, it can't know where a sample was recorded, so samples from travel get the given
zone. Samples that carry an offset, pre-aggregated daily totals, other providers' samples and events
(sleep, workouts) are left alone. A zone PostgreSQL doesn't know is reported and its user skipped.

The day rows rolled up under the UTC reading are deleted, and the old (UTC) and new local day of every
changed sample are queued for the rollup, which rebuilds them; reads fall back to the samples meanwhile.
One source is changed a week at a time and each week commits on its own, so a run can be interrupted and
run again: only samples still without an offset are touched, which also makes the script idempotent.
Not wired into startup, since it needs the zones as input.

Usage (inside Docker):
    docker compose exec app uv run python scripts/data_migrations/backfill_apple_zone_offset.py \\
        --zones-file zones.csv --dry-run
    docker compose exec app uv run python scripts/data_migrations/backfill_apple_zone_offset.py \\
        --zone <user_id>=America/New_York
"""

import argparse
import csv
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.integrations.rollup_queue import mark_dirty_days
from app.repositories.local_day import every_series_type_sql
from app.schemas.enums import ComputedBy, PeriodKind, ProviderName

WINDOW = timedelta(days=7)

_KNOWN_ZONE = text("SELECT EXISTS (SELECT 1 FROM pg_timezone_names WHERE name = :zone)")

_SOURCES = text("SELECT id FROM data_source WHERE user_id = :user_id AND provider = :provider ORDER BY id")

_UNSET = "s.zone_offset IS NULL AND s.is_daily_total IS NOT TRUE"

_SPAN = text(f"""
    SELECT min(s.recorded_at) AS first_at, max(s.recorded_at) AS last_at
    FROM series_sample s WHERE s.data_source_id = :source_id AND {_UNSET}
""")  # noqa: S608

# The zone's offset at each sample's own instant, as '+HH:MM'.
_TARGETS = f"""
    SELECT id, series_type_definition_id, recorded_at,
        CASE WHEN m < 0 THEN '-' ELSE '+' END
            || lpad((abs(m) / 60)::text, 2, '0') || ':' || lpad((abs(m) % 60)::text, 2, '0') AS zone_offset
    FROM (
        SELECT s.id, s.series_type_definition_id, s.recorded_at,
            (extract(epoch FROM (s.recorded_at AT TIME ZONE :zone) - (s.recorded_at AT TIME ZONE 'UTC')) / 60)::int
                AS m
        FROM series_sample s
        WHERE s.data_source_id = :source_id AND {every_series_type_sql("s")}
            AND s.recorded_at >= :lo AND s.recorded_at < :hi AND {_UNSET}
    ) offsets
"""  # noqa: S608

_DAYS = """
    SELECT series_type_definition_id AS type_id, (recorded_at AT TIME ZONE 'UTC')::date AS old_day,
        ((recorded_at AT TIME ZONE 'UTC') + zone_offset::interval)::date AS new_day, count(*) AS samples
    FROM {rows} GROUP BY 1, 2, 3
"""

_PREVIEW = text(f"WITH targets AS ({_TARGETS}) " + _DAYS.format(rows="targets"))  # noqa: S608

_UPDATE = text(
    f"""
    WITH targets AS ({_TARGETS}),
    changed AS (
        UPDATE series_sample s SET zone_offset = t.zone_offset FROM targets t WHERE s.id = t.id
        RETURNING s.series_type_definition_id, s.recorded_at, s.zone_offset
    )
    """  # noqa: S608
    + _DAYS.format(rows="changed")
)

# The rows rolled up while the samples read as UTC. The rollup keeps a day row whose samples are all gone, so a
# UTC day the samples left entirely (the evening of the last day, or of a day before a gap) would keep its row.
_DELETE_UTC_DAYS = text("""
    DELETE FROM series_aggregate a
    USING unnest(CAST(:days AS date[]), CAST(:type_ids AS integer[])) AS stale(day, type_id)
    WHERE a.data_source_id = :source_id AND a.computed_by = :computed_by AND a.period_kind = :period_kind
        AND a.series_type_definition_id = stale.type_id AND a.zone_offset = '+00:00'
        AND a.period_start = (stale.day::timestamp AT TIME ZONE 'UTC')
""")


def read_zones(pairs: Iterable[str], zones_file: Path | None) -> dict[str, str]:
    """User id to IANA zone, from ``USER_ID=ZONE`` pairs and a ``user_id,timezone`` CSV; blanks dropped."""
    rows: list[tuple[str, str]] = []
    for pair in pairs:
        user_id, sep, zone = pair.partition("=")
        if not sep:
            raise argparse.ArgumentTypeError(f"expected USER_ID=ZONE, got {pair!r}")
        rows.append((user_id, zone))
    if zones_file is not None:
        with zones_file.open(newline="") as handle:
            rows += [(row[0], row[1]) for row in csv.reader(handle) if len(row) >= 2]
    zones = {user_id.strip(): zone.strip() for user_id, zone in rows}
    zones.pop("user_id", None)
    return {user_id: zone for user_id, zone in zones.items() if user_id and zone}


def backfill_apple_zone_offset(db: Session, zones: dict[str, str], *, dry_run: bool) -> dict[str, int]:
    """Set the offset of every listed user's Apple samples that lack one, a committed week at a time."""
    counts = {"users": 0, "unknown_zone": 0, "no_apple_source": 0, "sources": 0, "samples": 0, "days_moved": 0}
    for user_id, zone in zones.items():
        if not db.execute(_KNOWN_ZONE, {"zone": zone}).scalar():
            print(f"  {zone!r} is not a zone PostgreSQL knows, user skipped")
            counts["unknown_zone"] += 1
            continue
        counts["users"] += 1
        sources = list(db.execute(_SOURCES, {"user_id": user_id, "provider": ProviderName.APPLE.value}).scalars())
        if not sources:
            counts["no_apple_source"] += 1
        for source_id in sources:
            counts["sources"] += 1
            samples, moved = _backfill_source(db, source_id, zone, dry_run=dry_run)
            counts["samples"] += samples
            counts["days_moved"] += moved
    print(("Would change: " if dry_run else "Changed: ") + "  ".join(f"{k}={v}" for k, v in counts.items()))
    return counts


def _backfill_source(db: Session, source_id: UUID, zone: str, *, dry_run: bool) -> tuple[int, int]:
    span = db.execute(_SPAN, {"source_id": source_id}).one()
    if span.first_at is None:
        return 0, 0
    samples = moved = 0
    lo: datetime = span.first_at
    while lo <= span.last_at:
        hi = lo + WINDOW
        params = {"source_id": source_id, "zone": zone, "lo": lo, "hi": hi}
        rows = db.execute(_PREVIEW if dry_run else _UPDATE, params).all()
        samples += sum(row.samples for row in rows)
        moved += sum(row.samples for row in rows if row.old_day != row.new_day)
        if not dry_run:
            stale = sorted({(row.old_day, row.type_id) for row in rows})
            if stale:
                db.execute(
                    _DELETE_UTC_DAYS,
                    {
                        "source_id": source_id,
                        "days": [day for day, _ in stale],
                        "type_ids": [type_id for _, type_id in stale],
                        "computed_by": ComputedBy.OPEN_WEARABLES,
                        "period_kind": PeriodKind.DAY,
                    },
                )
            days = {(source_id, row.old_day) for row in rows} | {(source_id, row.new_day) for row in rows}
            mark_dirty_days(db, days)
            db.commit()
        lo = hi
    return samples, moved


def main(zones: dict[str, str], dry_run: bool) -> None:
    if not zones:
        print("No zones given, nothing to do.")
        return
    with SessionLocal() as db:
        backfill_apple_zone_offset(db, zones, dry_run=dry_run)
    if dry_run:
        print("Dry run, no changes made.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--zone", action="append", default=[], metavar="USER_ID=ZONE", help="A user's IANA zone")
    parser.add_argument("--zones-file", type=Path, help="CSV of user_id,timezone rows")
    parser.add_argument("--dry-run", action="store_true", help="Count affected samples without modifying data")
    args = parser.parse_args()
    main(read_zones(args.zone, args.zones_file), dry_run=args.dry_run)
