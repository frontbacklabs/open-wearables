from fastapi import APIRouter
from sqlalchemy import text

from app.database import DbSession, async_engine, engine

healthcheck_router = APIRouter()


def get_pool_status() -> dict[str, str]:
    """Get sync connection pool status for monitoring."""
    pool = engine.pool
    return {
        "max_pool_size": str(pool.size()),  # ty:ignore[unresolved-attribute]
        "connections_ready_for_reuse": str(pool.checkedin()),  # ty:ignore[unresolved-attribute]
        "active_connections": str(pool.checkedout()),  # ty:ignore[unresolved-attribute]
        "overflow": str(pool.overflow()),  # ty:ignore[unresolved-attribute]
    }


def get_async_pool_status() -> dict[str, str]:
    """Get async connection pool status for monitoring.

    Reported separately because it is a second, independent pool over the same
    database: its connections count against the server's max_connections just as
    the sync pool's do, and omitting it here hides up to half of what this process
    is actually holding.
    """
    pool = async_engine.sync_engine.pool
    return {
        "max_pool_size": str(pool.size()),  # ty:ignore[unresolved-attribute]
        "connections_ready_for_reuse": str(pool.checkedin()),  # ty:ignore[unresolved-attribute]
        "active_connections": str(pool.checkedout()),  # ty:ignore[unresolved-attribute]
        "overflow": str(pool.overflow()),  # ty:ignore[unresolved-attribute]
    }


@healthcheck_router.get("/db")
async def database_health(db: DbSession) -> dict[str, str | dict[str, str]]:
    """Database health check endpoint."""
    try:
        # Test connection
        db.execute(text("SELECT 1"))

        return {
            "status": "healthy",
            "pool": get_pool_status(),
            "async_pool": get_async_pool_status(),
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e),
        }
