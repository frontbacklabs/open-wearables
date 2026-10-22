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


class TestRefreshTokenReuseWindow:
    """Tests for the rotation grace window: a recently rotated token re-presents its successor."""

    def test_rotation_links_old_token_to_successor(self, db: Session) -> None:
        """Rotating a token records rotated_to_id on the revoked row."""
        # Arrange
        user = UserFactory()
        old_refresh_token = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")

        # Act
        result = refresh_token_service.refresh_token(db, old_refresh_token)

        # Assert
        db_token = db.query(RefreshToken).filter(RefreshToken.id == old_refresh_token).first()
        assert db_token is not None
        assert db_token.revoked_at is not None
        assert db_token.rotated_to_id == result.refresh_token

    def test_reuse_within_window_returns_successor_pair(self, db: Session) -> None:
        """Re-presenting a recently rotated token returns the live successor, not a 401."""
        # Arrange
        user = UserFactory()
        old_refresh_token = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")
        rotated = refresh_token_service.refresh_token(db, old_refresh_token)

        # Act — simulate the client retrying with the token it already had.
        result = refresh_token_service.refresh_token(db, old_refresh_token)

        # Assert — same refresh token id as the lost response carried; a fresh access token.
        assert result.refresh_token == rotated.refresh_token
        assert result.access_token is not None
        assert result.token_type == "bearer"

    def test_reuse_walks_chain_to_head(self, db: Session) -> None:
        """A token several rotations behind resolves to the current chain head."""
        # Arrange
        user = UserFactory()
        t0 = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")
        r1 = refresh_token_service.refresh_token(db, t0)
        r2 = refresh_token_service.refresh_token(db, r1.refresh_token)

        # Act — client lost both responses and still holds t0.
        result = refresh_token_service.refresh_token(db, t0)

        # Assert
        assert result.refresh_token == r2.refresh_token

    def test_reuse_after_window_raises_401(self, db: Session) -> None:
        """A rotated token presented past the window still fails."""
        # Arrange
        user = UserFactory()
        old_refresh_token = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")
        refresh_token_service.refresh_token(db, old_refresh_token)

        db_token = db.query(RefreshToken).filter(RefreshToken.id == old_refresh_token).first()
        db_token.revoked_at = datetime.now(timezone.utc) - timedelta(
            seconds=settings.refresh_token_reuse_window_seconds + 60
        )
        db.commit()

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            refresh_token_service.refresh_token(db, old_refresh_token)
        assert exc_info.value.status_code == 401

    def test_reuse_when_head_revoked_raises_401(self, db: Session) -> None:
        """Reuse cannot resurrect a chain whose head was itself revoked."""
        # Arrange — rotate, then explicitly revoke the successor (e.g. disconnect).
        user = UserFactory()
        old_refresh_token = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")
        rotated = refresh_token_service.refresh_token(db, old_refresh_token)
        refresh_token_service.revoke_token(db, rotated.refresh_token)

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            refresh_token_service.refresh_token(db, old_refresh_token)
        assert exc_info.value.status_code == 401

    def test_explicitly_revoked_token_still_raises_401(self, db: Session) -> None:
        """Tokens revoked outside rotation (logout) have no successor to re-present."""
        # Covered by test_refresh_revoked_token_raises_401: revoke_token() never sets
        # rotated_to_id, so the chain walk finds no head.
        user = UserFactory()
        refresh_token = refresh_token_service.create_sdk_refresh_token(db, user.id, "test_app")
        refresh_token_service.revoke_token(db, refresh_token)
        db_token = db.query(RefreshToken).filter(RefreshToken.id == refresh_token).first()
        assert db_token.rotated_to_id is None

    def test_reuse_works_for_developer_tokens(self, db: Session) -> None:
        """Developer refresh tokens get the same reuse-window behavior."""
        # Arrange
        developer = DeveloperFactory()
        old_refresh_token = refresh_token_service.create_developer_refresh_token(db, developer.id)
        rotated = refresh_token_service.refresh_token(db, old_refresh_token)

        # Act
        result = refresh_token_service.refresh_token(db, old_refresh_token)

        # Assert
        assert result.refresh_token == rotated.refresh_token


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
