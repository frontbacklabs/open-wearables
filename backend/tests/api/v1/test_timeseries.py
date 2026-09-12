"""Tests for the timeseries endpoint."""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.factories import (
    ApiKeyFactory,
    DataPointSeriesFactory,
    DataSourceFactory,
    SeriesTypeDefinitionFactory,
    UserFactory,
)
from tests.utils import api_key_headers


class TestTimeSeriesEndpoint:
    """Test suite for the timeseries endpoint."""

    BASE_PARAMS = {
        "start_time": "2026-01-01T00:00:00Z",
        "end_time": "2026-01-02T00:00:00Z",
        "types": ["heart_rate"],
    }

    def _url(self, user_id: object) -> str:
        return f"/api/v1/users/{user_id}/timeseries"

    def _make_samples(self, user: object, count: int = 5) -> list[datetime]:
        """Create `count` heart-rate samples spaced 1 minute apart (ascending)."""
        data_source = DataSourceFactory(user=user)
        series_type = SeriesTypeDefinitionFactory.get_or_create_heart_rate()
        base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        timestamps = []
        for i in range(count):
            ts = base + timedelta(minutes=i)
            DataPointSeriesFactory(data_source=data_source, series_type=series_type, recorded_at=ts, value=60 + i)
            timestamps.append(ts)
        return timestamps

    def test_sort_order_desc_returns_latest_first(self, client: TestClient, db: Session) -> None:
        """sort_order=desc returns the newest samples on the first page."""
        user = UserFactory()
        self._make_samples(user, count=3)
        api_key = ApiKeyFactory()

        response = client.get(
            self._url(user.id),
            headers=api_key_headers(api_key.id),
            params={**self.BASE_PARAMS, "sort_order": "desc"},
        )

        assert response.status_code == 200
        values = [item["value"] for item in response.json()["data"]]
        assert values == [62.0, 61.0, 60.0]

    def test_sort_order_asc_returns_earliest_first(self, client: TestClient, db: Session) -> None:
        """Default (asc) ordering keeps the previous earliest-first behaviour."""
        user = UserFactory()
        self._make_samples(user, count=3)
        api_key = ApiKeyFactory()

        response = client.get(
            self._url(user.id),
            headers=api_key_headers(api_key.id),
            params=self.BASE_PARAMS,
        )

        assert response.status_code == 200
        values = [item["value"] for item in response.json()["data"]]
        assert values == [60.0, 61.0, 62.0]

    def test_sort_order_desc_paginates_forward(self, client: TestClient, db: Session) -> None:
        """next_cursor continues in descending order."""
        user = UserFactory()
        self._make_samples(user, count=5)
        api_key = ApiKeyFactory()

        response = client.get(
            self._url(user.id),
            headers=api_key_headers(api_key.id),
            params={**self.BASE_PARAMS, "sort_order": "desc", "limit": 2},
        )

        assert response.status_code == 200
        data = response.json()
        assert [item["value"] for item in data["data"]] == [64.0, 63.0]
        assert data["pagination"]["has_more"] is True

        next_page = client.get(
            self._url(user.id),
            headers=api_key_headers(api_key.id),
            params={
                **self.BASE_PARAMS,
                "sort_order": "desc",
                "limit": 2,
                "cursor": data["pagination"]["next_cursor"],
            },
        )

        assert next_page.status_code == 200
        assert [item["value"] for item in next_page.json()["data"]] == [62.0, 61.0]

    def test_invalid_sort_order_rejected(self, client: TestClient, db: Session) -> None:
        """Invalid sort_order values are rejected."""
        user = UserFactory()
        api_key = ApiKeyFactory()

        response = client.get(
            self._url(user.id),
            headers=api_key_headers(api_key.id),
            params={**self.BASE_PARAMS, "sort_order": "newest"},
        )

        assert response.status_code == 400
