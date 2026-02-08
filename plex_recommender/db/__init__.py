"""Database connection and session management."""

from collections.abc import Generator
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor

from plex_recommender.config import get_settings
from plex_recommender.logging import get_logger

logger = get_logger(__name__)


def get_connection_params() -> dict:
    """Get database connection parameters from settings."""
    settings = get_settings()
    # Convert to string and parse, as Pydantic v2 MultiHostUrl has different accessors
    url_str = str(settings.database_url)

    # Parse the URL manually for compatibility
    from urllib.parse import urlparse

    parsed = urlparse(url_str)

    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 5432,
        "database": parsed.path.lstrip("/") if parsed.path else "plex_recommender",
        "user": parsed.username,
        "password": parsed.password,
    }


@contextmanager
def get_db_connection() -> Generator[psycopg2.extensions.connection, None, None]:
    """Get a database connection context manager."""
    conn = None
    try:
        conn = psycopg2.connect(**get_connection_params())
        yield conn
    except psycopg2.Error as e:
        logger.error("database_connection_error", error=str(e))
        raise
    finally:
        if conn:
            conn.close()


@contextmanager
def get_db_cursor(
    commit: bool = True,
) -> Generator[psycopg2.extensions.cursor, None, None]:
    """Get a database cursor with optional auto-commit."""
    with get_db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        try:
            yield cursor
            if commit:
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()


def init_db() -> None:
    """Initialize database schema and run migrations."""
    from plex_recommender.db.migrations import run_migrations
    from plex_recommender.db.schema import create_tables

    create_tables()
    logger.info("database_schema_created")

    # Run any pending migrations
    count = run_migrations()
    if count > 0:
        logger.info("migrations_applied", count=count)

    logger.info("database_initialized")


def is_db_initialized() -> bool:
    """Check if the database has been initialized (users table exists)."""
    try:
        with get_db_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_name = 'users'
                );
            """)
            result = cursor.fetchone()
            return result["exists"] if result else False
    except Exception:
        return False


def ensure_db_initialized() -> None:
    """Ensure database is initialized, auto-init if empty."""
    if not is_db_initialized():
        logger.info("database_not_initialized", action="auto_initializing")
        init_db()
    else:
        # DB exists, just run any pending migrations
        from plex_recommender.db.migrations import run_migrations

        count = run_migrations()
        if count > 0:
            logger.info("migrations_applied", count=count)
