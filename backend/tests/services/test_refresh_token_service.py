"""
Unit tests for refresh token service.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.models import RefreshToken
from app.schemas.auth import TokenType
from app.services.refresh_token_service import refresh_token_service
from tests.factories import DeveloperFactory, UserFactory


class TestCreateSDKRefreshToken:
    """Tests for create_sdk_refresh_token."""

    def test_create_sdk_refresh_token_format(self, db: Session) -> None:
        """SDK refresh token should have rt- prefix and be 35 characters total."""
        # Arrange
        user = UserFactory()
        app_id = "test_app_123"

        # Act
        token = refresh_token_service.create_sdk_refresh_token(db, user.id, app_id)

        # Assert
        assert token.startswith("rt-")
        assert len(token) == 35  # "rt-" (3) + 32 hex chars

    def test_create_sdk_refresh_token_stored_in_db(self, db: Session) -> None:
        """SDK refresh token should be stored in database with correct metadata."""
        # Arrange
        user = UserFactory()
        app_id = "test_app_123"

        # Act
        token = refresh_token_service.create_sdk_refresh_token(db, user.id, app_id)

        # Assert
        db_token = db.query(RefreshToken).filter(RefreshToken.id == token).first()
        assert db_token is not None
        assert db_token.token_type == TokenType.SDK
        assert db_token.user_id == user.id
        assert db_token.app_id == app_id
        assert db_token.developer_id is None
        assert db_token.revoked_at is None


class TestCreateDeveloperRefreshToken:
    """Tests for create_developer_refresh_token."""

    def test_create_developer_refresh_token_format(self, db: Session) -> None:
        """Developer refresh token should have rt- prefix and be 35 characters total."""
        # Arrange
        developer = DeveloperFactory()

        # Act
        token = refresh_token_service.create_developer_refresh_token(db, developer.id)

        # Assert
        assert token.startswith("rt-")
        assert len(token) == 35  # "rt-" (3) + 32 hex chars

    def test_create_developer_refresh_token_stored_in_db(self, db: Session) -> None:
        """Developer refresh token should be stored in database with correct metadata."""
        # Arrange
        developer = DeveloperFactory()

        # Act
        token = refresh_token_service.create_developer_refresh_token(db, developer.id)

        # Assert
        db_token = db.query(RefreshToken).filter(RefreshToken.id == token).first()
        assert db_token is not None
        assert db_token.token_type == TokenType.DEVELOPER
        assert db_token.developer_id == developer.id
        assert db_token.user_id is None
        assert db_token.app_id is None
        assert db_token.revoked_at is None


class TestRefreshToken:
    """Tests for refresh_token method."""

    def test_refresh_sdk_token_success(self, db: Session) -> None:
        """Refreshing SDK token should return new access token and rotated refresh token."""
        # Arrange
        user = UserFactory()
        app_id = "test_app_123"
        refresh_token = refresh_token_service.create_sdk_refresh_token(db, user.id, app_id)

        # Act
        result = refresh_token_service.refresh_token(db, refresh_token)

        # Assert
        assert result.access_token is not None
        assert result.token_type == "bearer"
        # Refresh token should be rotated
        assert result.refresh_token != refresh_token
        assert result.refresh_token.startswith("rt-")

    def test_refresh_developer_token_success(self, db: Session) -> None:
        """Refreshing developer token should return new access token and rotated refresh token."""
        # Arrange
        developer = DeveloperFactory()
        refresh_token = refresh_token_service.create_developer_refresh_token(db, developer.id)

        # Act
        result = refresh_token_service.refresh_token(db, refresh_token)

        # Assert
        assert result.access_token is not None
        assert result.token_type == "bearer"
        # Refresh token should be rotated
        assert result.refresh_token != refresh_token
        assert result.refresh_token.startswith("rt-")

    def test_refresh_invalid_token_raises_401(self, db: Session) -> None:
        """Refreshing invalid token should raise 401."""
        # Arrange
        invalid_token = "rt-invalidtoken12345678901234567890"

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            refresh_token_service.refresh_token(db, invalid_token)

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Invalid or revoked refresh token"

    def test_refresh_revoked_token_raises_401(self, db: Session) -> None:
        """Refreshing revoked token should raise 401."""
        # Arrange
        user = UserFactory()
        refresh_token = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")
        refresh_token_service.revoke_token(db, refresh_token)

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            refresh_token_service.refresh_token(db, refresh_token)

        assert exc_info.value.status_code == 401

    def test_refresh_revokes_old_token(self, db: Session) -> None:
        """Refreshing token should revoke the old token (rotation)."""
        # Arrange
        user = UserFactory()
        old_refresh_token = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")

        # Verify not revoked initially
        db_token = db.query(RefreshToken).filter(RefreshToken.id == old_refresh_token).first()
        assert db_token is not None
        assert db_token.revoked_at is None

        # Act
        result = refresh_token_service.refresh_token(db, old_refresh_token)

        # Assert - old token is revoked
        db.refresh(db_token)
        assert db_token.revoked_at is not None

        # Assert - new token exists and is not revoked
        new_db_token = db.query(RefreshToken).filter(RefreshToken.id == result.refresh_token).first()
        assert new_db_token is not None
        assert new_db_token.revoked_at is None


class TestRotatedTokenGrace:
    """Tests for replaying a rotated-out token inside the grace window."""

    def test_rotated_token_replay_returns_successor(self, db: Session) -> None:
        """A token rotated out moments ago returns its already-issued successor."""
        # Arrange
        user = UserFactory()
        refresh_token = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")
        first = refresh_token_service.refresh_token(db, refresh_token)

        # Act - replay the old token, simulating a lost rotation response
        replay = refresh_token_service.refresh_token(db, refresh_token)

        # Assert - same successor, fresh access token, no further rotation
        assert replay.refresh_token == first.refresh_token
        assert replay.access_token is not None
        assert replay.token_type == "bearer"

    def test_rotated_token_replay_keeps_successor_live(self, db: Session) -> None:
        """The successor returned by a grace replay is still usable for rotation."""
        # Arrange
        user = UserFactory()
        refresh_token = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")
        first = refresh_token_service.refresh_token(db, refresh_token)
        refresh_token_service.refresh_token(db, refresh_token)  # grace replay

        # Act - client rotates onward from the successor it adopted
        second = refresh_token_service.refresh_token(db, first.refresh_token)

        # Assert
        assert second.refresh_token != first.refresh_token

    def test_rotated_token_replay_after_grace_raises_401(self, db: Session) -> None:
        """A token rotated out longer than the grace window is a hard 401."""
        # Arrange
        user = UserFactory()
        refresh_token = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")
        refresh_token_service.refresh_token(db, refresh_token)
        db_token = db.query(RefreshToken).filter(RefreshToken.id == refresh_token).first()
        assert db_token is not None
        db_token.revoked_at = datetime.now(timezone.utc) - timedelta(
            seconds=settings.refresh_token_rotation_grace_seconds + 60
        )
        db.commit()

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            refresh_token_service.refresh_token(db, refresh_token)

        assert exc_info.value.status_code == 401

    def test_explicitly_revoked_token_has_no_grace(self, db: Session) -> None:
        """A token revoked outside rotation has no successor and stays a hard 401."""
        # Arrange
        user = UserFactory()
        refresh_token = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")
        refresh_token_service.revoke_token(db, refresh_token)

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            refresh_token_service.refresh_token(db, refresh_token)

        assert exc_info.value.status_code == 401

    def test_replay_follows_rotation_chain_to_live_tail(self, db: Session) -> None:
        """Replaying a token several rotations deep returns the live tail token."""
        # Arrange
        user = UserFactory()
        first_token = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")
        second = refresh_token_service.refresh_token(db, first_token)
        third = refresh_token_service.refresh_token(db, second.refresh_token)

        # Act - replay the two-deep stale token
        replay = refresh_token_service.refresh_token(db, first_token)

        # Assert
        assert replay.refresh_token == third.refresh_token

    def test_replay_after_successor_revoked_raises_401(self, db: Session) -> None:
        """No grace when the whole chain is dead (e.g. revoke_all_for_user)."""
        # Arrange
        user = UserFactory()
        refresh_token = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")
        first = refresh_token_service.refresh_token(db, refresh_token)
        refresh_token_service.revoke_token(db, first.refresh_token)

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            refresh_token_service.refresh_token(db, refresh_token)

        assert exc_info.value.status_code == 401


class TestRevokeToken:
    """Tests for revoke_token method."""

    def test_revoke_token_success(self, db: Session) -> None:
        """Revoking token should set revoked_at timestamp."""
        # Arrange
        user = UserFactory()
        refresh_token = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")

        # Act
        result = refresh_token_service.revoke_token(db, refresh_token)

        # Assert
        assert result is True
        db_token = db.query(RefreshToken).filter(RefreshToken.id == refresh_token).first()
        assert db_token is not None
        assert db_token.revoked_at is not None

    def test_revoke_nonexistent_token_raises_404(self, db: Session) -> None:
        """Revoking non-existent token should raise 404."""
        # Arrange
        invalid_token = "rt-nonexistent123456789012345678"

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            refresh_token_service.revoke_token(db, invalid_token)

        assert exc_info.value.status_code == 404

    def test_revoke_already_revoked_token_raises_404(self, db: Session) -> None:
        """Revoking already revoked token should raise 404."""
        # Arrange
        user = UserFactory()
        refresh_token = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")
        refresh_token_service.revoke_token(db, refresh_token)

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            refresh_token_service.revoke_token(db, refresh_token)

        assert exc_info.value.status_code == 404
