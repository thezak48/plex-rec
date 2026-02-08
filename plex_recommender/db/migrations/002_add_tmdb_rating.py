"""
Migration: Add tmdb_rating column to library_content.

TMDB provides vote_average (0-10 scale) which can supplement Plex ratings
for better recommendations.
"""

from plex_recommender.db import get_db_cursor
from plex_recommender.logging import get_logger

logger = get_logger(__name__)


def upgrade() -> None:
    """Add tmdb_rating column to library_content."""
    migration_sql = """
    DO $$
    BEGIN
        -- Check if library_content table exists first
        IF NOT EXISTS (SELECT 1 FROM information_schema.tables
                       WHERE table_name = 'library_content') THEN
            RAISE NOTICE 'library_content table does not exist, skipping';
            RETURN;
        END IF;

        -- Add tmdb_rating column if it doesn't exist
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name = 'library_content' AND column_name = 'tmdb_rating') THEN
            ALTER TABLE library_content ADD COLUMN tmdb_rating DECIMAL(3, 1);
            RAISE NOTICE 'Added tmdb_rating column';
        END IF;

        -- Add tmdb_id column for caching TMDB lookups
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name = 'library_content' AND column_name = 'tmdb_id') THEN
            ALTER TABLE library_content ADD COLUMN tmdb_id INTEGER;
            RAISE NOTICE 'Added tmdb_id column';
        END IF;
    END $$;
    """
    with get_db_cursor() as cursor:
        cursor.execute(migration_sql)

    logger.info("migration_002_complete", description="Added tmdb_rating, tmdb_id columns")


def downgrade() -> None:
    """Remove tmdb_rating, tmdb_id columns from library_content."""
    downgrade_sql = """
    ALTER TABLE library_content
        DROP COLUMN IF EXISTS tmdb_rating,
        DROP COLUMN IF EXISTS tmdb_id;
    """
    with get_db_cursor() as cursor:
        cursor.execute(downgrade_sql)
