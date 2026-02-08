"""
Migration: Add actors, keywords, languages columns to library_content.

These columns support the recommendation weights system by storing
additional metadata for LLM-based recommendations.
"""

from plex_recommender.db import get_db_cursor
from plex_recommender.logging import get_logger

logger = get_logger(__name__)


def upgrade() -> None:
    """Add actors, keywords, languages columns to library_content if they don't exist."""
    migration_sql = """
    DO $$
    BEGIN
        -- Check if library_content table exists first
        IF NOT EXISTS (SELECT 1 FROM information_schema.tables
                       WHERE table_name = 'library_content') THEN
            RAISE NOTICE 'library_content table does not exist, skipping';
            RETURN;
        END IF;

        -- Add actors column if it doesn't exist
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name = 'library_content' AND column_name = 'actors') THEN
            ALTER TABLE library_content ADD COLUMN actors TEXT[];
            RAISE NOTICE 'Added actors column';
        END IF;

        -- Add keywords column if it doesn't exist
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name = 'library_content' AND column_name = 'keywords') THEN
            ALTER TABLE library_content ADD COLUMN keywords TEXT[];
            RAISE NOTICE 'Added keywords column';
        END IF;

        -- Add languages column if it doesn't exist
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name = 'library_content' AND column_name = 'languages') THEN
            ALTER TABLE library_content ADD COLUMN languages TEXT[];
            RAISE NOTICE 'Added languages column';
        END IF;
    END $$;
    """
    with get_db_cursor() as cursor:
        cursor.execute(migration_sql)

    logger.info("migration_001_complete", description="Added actors, keywords, languages columns")


def downgrade() -> None:
    """Remove actors, keywords, languages columns from library_content."""
    downgrade_sql = """
    ALTER TABLE library_content
        DROP COLUMN IF EXISTS actors,
        DROP COLUMN IF EXISTS keywords,
        DROP COLUMN IF EXISTS languages;
    """
    with get_db_cursor() as cursor:
        cursor.execute(downgrade_sql)

    logger.info("migration_001_reverted", description="Removed actors, keywords, languages columns")
