import secrets
from datetime import datetime, timezone
from logging import Logger, getLogger
from uuid import UUID

from fastapi import HTTPException, status

from app.config import settings
from app.database import DbSession
from app.models import RefreshToken
from app.repositories.refresh_token_repository import refresh_token_repository
from app.schemas.auth import TokenResponse, TokenType
from app.services.sdk_token_service import create_sdk_user_token
from app.utils.security import create_access_token


class RefreshTokenService:
    """Service for managing refresh tokens."""

    def __init__(self, log: Logger) -> None:
        self.logger = log
        self.repo = refresh_token_repository

    @staticmethod
    def _generate_refresh_token_id() -> str:
        """Generate an opaque refresh token ID with rt- prefix."""
        return f"rt-{secrets.token_hex(16)}"

    def create_sdk_refresh_token(self, db_session: DbSession, user_id: str, app_id: str) -> str:
        """Create a refresh token for an SDK token.

        Args:
            db_session: Database session
            user_id: The OpenWearables User ID
            app_id: The application ID that created the token

        Returns:
            The refresh token string (rt-{hex})
        """
        token_id = self._generate_refresh_token_id()
        token = RefreshToken(
            id=token_id,
            token_type=TokenType.SDK,
            user_id=user_id,
            app_id=app_id,
            developer_id=None,
            created_at=datetime.now(timezone.utc),
            last_used_at=None,
            revoked_at=None,
        )
        self.repo.create(db_session, token)
        self.logger.debug(f"Created SDK refresh token for user {user_id}, app {app_id}")
        return token_id

    def create_developer_refresh_token(self, db_session: DbSession, developer_id: UUID) -> str:
        """Create a refresh token for a developer token.

        Args:
            db_session: Database session
            developer_id: The developer ID

        Returns:
            The refresh token string (rt-{hex})
        """
        token_id = self._generate_refresh_token_id()
        token = RefreshToken(
            id=token_id,
            token_type=TokenType.DEVELOPER,
            user_id=None,
            app_id=None,
            developer_id=developer_id,
            created_at=datetime.now(timezone.utc),
            last_used_at=None,
            revoked_at=None,
        )
        self.repo.create(db_session, token)
        self.logger.debug(f"Created developer refresh token for developer {developer_id}")
        return token_id

    def refresh_token(self, db_session: DbSession, refresh_token_str: str) -> TokenResponse:
        """Exchange a refresh token for a new access token.

        Implements refresh token rotation: the old refresh token is revoked and
        a new one is issued with each refresh request.

        Args:
            db_session: Database session
            refresh_token_str: The refresh token string

        Returns:
            TokenResponse with new access token and new refresh token

        Raises:
            HTTPException: If the refresh token is invalid or revoked
        """
        token = self.repo.get_valid_token(db_session, refresh_token_str)
        if not token:
            successor = self._grace_successor(db_session, refresh_token_str)
            if not successor:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid or revoked refresh token",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            # The presented token was already rotated out, but its owner never
            # received the response. Return the live successor instead of
            # rotating again so the retry completes the missed rotation.
            self.logger.debug("Replaying rotated-out refresh token within grace window")
            return TokenResponse(
                access_token=self._create_access_token(successor),
                token_type="bearer",
                refresh_token=successor.id,
                expires_in=settings.access_token_expire_minutes * 60,
            )

        # Rotate: mint the successor, then record it on the revoked token.
        new_refresh_token = self._create_successor_refresh_token(db_session, token)
        self.repo.rotate_token(db_session, token, new_refresh_token)
        return TokenResponse(
            access_token=self._create_access_token(token),
            token_type="bearer",
            refresh_token=new_refresh_token,
            expires_in=settings.access_token_expire_minutes * 60,
        )

    def _create_access_token(self, token: RefreshToken) -> str:
        """Mint an access token for a refresh token's identity."""
        if token.token_type == TokenType.SDK:
            return create_sdk_user_token(
                app_id=token.app_id,  # ty:ignore[invalid-argument-type]
                user_id=str(token.user_id),
            )
        if token.token_type == TokenType.DEVELOPER:
            return create_access_token(subject=str(token.developer_id))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown token type: {token.token_type}",
        )

    def _create_successor_refresh_token(self, db_session: DbSession, token: RefreshToken) -> str:
        """Mint the next refresh token in a token's rotation chain."""
        if token.token_type == TokenType.SDK:
            new_refresh_token = self.create_sdk_refresh_token(
                db_session,
                user_id=token.user_id,  # ty:ignore[invalid-argument-type]
                app_id=token.app_id,  # ty:ignore[invalid-argument-type]
            )
            self.logger.debug(f"Refreshed SDK token for user {token.user_id} (rotated)")
            return new_refresh_token
        if token.token_type == TokenType.DEVELOPER:
            new_refresh_token = self.create_developer_refresh_token(
                db_session,
                developer_id=token.developer_id,  # ty:ignore[invalid-argument-type]
            )
            self.logger.debug(f"Refreshed developer token for developer {token.developer_id} (rotated)")
            return new_refresh_token
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown token type: {token.token_type}",
        )

    def _grace_successor(self, db_session: DbSession, refresh_token_str: str) -> RefreshToken | None:
        """Resolve the live successor of a rotated-out token inside the grace window.

        Returns the token the client should adopt, or None when the presented
        token was not rotated (invalid, or explicitly revoked), the grace window
        elapsed, or the rotation chain no longer ends at a live token.
        """
        if settings.refresh_token_rotation_grace_seconds <= 0:
            return None
        token = self.repo.get_token(db_session, refresh_token_str)
        if not token or token.rotated_to_id is None or token.revoked_at is None:
            return None
        revoked_at = token.revoked_at
        if revoked_at.tzinfo is None:
            revoked_at = revoked_at.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - revoked_at).total_seconds()
        if age > settings.refresh_token_rotation_grace_seconds:
            return None
        # Follow the rotation chain to its live tail so a stale replay still
        # resolves when the immediate successor was itself rotated onward.
        live = token
        for _ in range(16):
            if live.rotated_to_id is None:
                break
            next_token = self.repo.get_token(db_session, live.rotated_to_id)
            if next_token is None:
                return None
            live = next_token
        if live.revoked_at is not None:
            return None
        return live

    def revoke_token(self, db_session: DbSession, refresh_token_str: str) -> bool:
        """Revoke a refresh token.

        Args:
            db_session: Database session
            refresh_token_str: The refresh token string

        Returns:
            True if the token was revoked, False if not found

        Raises:
            HTTPException: If the refresh token is not found
        """
        token = self.repo.get_valid_token(db_session, refresh_token_str)
        if not token:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Refresh token not found",
            )

        self.repo.revoke_token(db_session, token)
        self.logger.debug(f"Revoked refresh token {refresh_token_str[:10]}...")
        return True


refresh_token_service = RefreshTokenService(log=getLogger(__name__))
