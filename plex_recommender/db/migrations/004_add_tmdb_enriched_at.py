"""
Migration 004: Add tmdb_enriched_at column to library_content table.

This column tracks when a library item was last enriched with TMDB data.
"""

from plex_recommender.db import get_db_cursor
from plex_recommender.logging import get_logger

logger = get_logger(__name__)

def upgrade() -> None:
    """Add tmdb_enriched_at column to library_content table."""
    migration_sql = """
    DO $$
    BEGIN
        -- Check if library_content table exists first
        IF NOT EXISTS (SELECT 1 FROM information_schema.tables
                       WHERE table_name = 'library_content') THEN
            RAISE NOTICE 'library_content table does not exist, skipping';
            RETURN;
        END IF;

        -- Add tmdb_enriched_at column if it doesn't exist
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name = 'library_content' AND column_name = 'tmdb_enriched_at') THEN
            ALTER TABLE library_content ADD COLUMN tmdb_enriched_at TIMESTAMP;
            RAISE NOTICE 'Added tmdb_enriched_at column';
        END IF;
    END $$;
    """
    with get_db_cursor() as cursor:
        cursor.execute(migration_sql)
    logger.info("migration_complete", migration="add_tmdb_enriched_at")

def downgrade() -> None:
    """Remove tmdb_enriched_at column from library_content table."""
    migration_sql = """
    DO $$
    BEGIN
        -- Check if library_content table exists first
        IF NOT EXISTS (SELECT 1 FROM information_schema.tables
                       WHERE table_name = 'library_content') THEN
            RAISE NOTICE 'library_content table does not exist, skipping';
            RETURN;
        END IF;

        -- Drop tmdb_enriched_at column if it exists
        IF EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'library_content' AND column_name = 'tmdb_enriched_at') THEN
            ALTER TABLE library_content DROP COLUMN tmdb_enriched_at;
            RAISE NOTICE 'Dropped tmdb_enriched_at column';
        END IF;
    END $$;
    """
    with get_db_cursor() as cursor:
        cursor.execute(migration_sql)
    logger.info("migration_reverted", migration="add_tmdb_enriched_at")