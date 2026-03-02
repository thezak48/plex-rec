"""
Migration 005: Add plex_view_count and plex_last_viewed_at to library_content table.

These columns store the global Plex view count and last viewed timestamp
populated during Plex sync. They are used to avoid sending already-watched
items to the LLM.
"""

from plex_recommender.db import get_db_cursor
from plex_recommender.logging import get_logger

logger = get_logger(__name__)


def upgrade() -> None:
    """Add plex_view_count and plex_last_viewed_at columns to library_content."""
    migration_sql = """
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM information_schema.tables
                       WHERE table_name = 'library_content') THEN
            RAISE NOTICE 'library_content table does not exist, skipping';
            RETURN;
        END IF;

        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name = 'library_content' AND column_name = 'plex_view_count') THEN
            ALTER TABLE library_content ADD COLUMN plex_view_count INTEGER DEFAULT 0;
            RAISE NOTICE 'Added plex_view_count column';
        END IF;

        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name = 'library_content' AND column_name = 'plex_last_viewed_at') THEN
            ALTER TABLE library_content ADD COLUMN plex_last_viewed_at TIMESTAMP WITH TIME ZONE;
            RAISE NOTICE 'Added plex_last_viewed_at column';
        END IF;
    END $$;
    """
    with get_db_cursor() as cursor:
        cursor.execute(migration_sql)
    logger.info("migration_complete", migration="add_plex_watched_columns")


def downgrade() -> None:
    """Remove plex_view_count and plex_last_viewed_at columns from library_content."""
    migration_sql = """
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM information_schema.tables
                       WHERE table_name = 'library_content') THEN
            RAISE NOTICE 'library_content table does not exist, skipping';
            RETURN;
        END IF;

        IF EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'library_content' AND column_name = 'plex_last_viewed_at') THEN
            ALTER TABLE library_content DROP COLUMN plex_last_viewed_at;
            RAISE NOTICE 'Dropped plex_last_viewed_at column';
        END IF;

        IF EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'library_content' AND column_name = 'plex_view_count') THEN
            ALTER TABLE library_content DROP COLUMN plex_view_count;
            RAISE NOTICE 'Dropped plex_view_count column';
        END IF;
    END $$;
    """
    with get_db_cursor() as cursor:
        cursor.execute(migration_sql)
    logger.info("migration_reverted", migration="add_plex_watched_columns")
