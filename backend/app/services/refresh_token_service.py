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
from app.utils.structured_logging import log_structured


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
        token = self.repo.get_token(db_session, refresh_token_str)
        if token is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or revoked refresh token",
                headers={"WWW-Authenticate": "Bearer"},
            )

        if token.revoked_at is not None:
            return self._reissue_chain_head(db_session, token)

        # Rotation: mint the successor first so the revocation can link to it.
        new_refresh_token = self._create_refresh_token(db_session, token)
        self.repo.revoke_token(db_session, token, rotated_to_id=new_refresh_token)
        access_token = self._create_access_token(token)
        self.logger.debug(f"Refreshed {token.token_type} token (rotated)")

        return TokenResponse(
            access_token=access_token,
            token_type="bearer",
            refresh_token=new_refresh_token,
            expires_in=settings.access_token_expire_minutes * 60,
        )

    def _create_access_token(self, token: RefreshToken) -> str:
        """Mint an access token matching the refresh token's type."""
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

    def _create_refresh_token(self, db_session: DbSession, token: RefreshToken) -> str:
        """Mint a refresh token matching the presented token's type."""
        if token.token_type == TokenType.SDK:
            return self.create_sdk_refresh_token(
                db_session,
                user_id=token.user_id,  # ty:ignore[invalid-argument-type]
                app_id=token.app_id,  # ty:ignore[invalid-argument-type]
            )
        if token.token_type == TokenType.DEVELOPER:
            return self.create_developer_refresh_token(
                db_session,
                developer_id=token.developer_id,  # ty:ignore[invalid-argument-type]
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown token type: {token.token_type}",
        )

    def _reissue_chain_head(self, db_session: DbSession, token: RefreshToken) -> TokenResponse:
        """Re-present the live successor when a client retries with a recently rotated token.

        Rotation revokes the presented token the moment the new pair is minted, so a
        client that lost the response (suspension, network drop, racing refreshes)
        would otherwise hold a permanently dead credential and 401 on every upload.
        Within ``refresh_token_reuse_window_seconds`` of the presented token's own
        revocation, walk ``rotated_to_id`` to the chain head and return that pair:
        the successor pair the lost response was carrying. Explicit revocations
        (logout, disconnect, account erasure) never set ``rotated_to_id``, so
        deliberately killed chains stay dead.
        """
        revoked_at = token.revoked_at
        if revoked_at is not None and revoked_at.tzinfo is None:
            revoked_at = revoked_at.replace(tzinfo=timezone.utc)
        within_window = (
            revoked_at is not None
            and (datetime.now(timezone.utc) - revoked_at).total_seconds() <= settings.refresh_token_reuse_window_seconds
        )

        head = token
        hops = 0
        while head.rotated_to_id is not None and hops < 32:
            successor = self.repo.get_token(db_session, head.rotated_to_id)
            if successor is None:
                break
            head = successor
            hops += 1

        if within_window and head.revoked_at is None and head is not token:
            log_structured(
                self.logger,
                "info",
                "Re-issued rotated refresh token chain head",
                action="refresh_token_reuse",
                token_type=str(token.token_type),
                chain_hops=hops,
            )
            return TokenResponse(
                access_token=self._create_access_token(head),
                token_type="bearer",
                refresh_token=head.id,
                expires_in=settings.access_token_expire_minutes * 60,
            )

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked refresh token",
            headers={"WWW-Authenticate": "Bearer"},
        )

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
