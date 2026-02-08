"""Migration 003: Add library_section_id to recommendations table.

This allows filtering recommendations by library and prevents deactivating
recommendations for other libraries when generating new ones.
"""

from plex_recommender.db import get_db_cursor
from plex_recommender.logging import get_logger

logger = get_logger(__name__)


def upgrade() -> None:
    """Add library_section_id column to recommendations table."""
    migration_sql = """
    DO $$
    BEGIN
        -- Check if recommendations table exists first
        IF NOT EXISTS (SELECT 1 FROM information_schema.tables
                       WHERE table_name = 'recommendations') THEN
            RAISE NOTICE 'recommendations table does not exist, skipping';
            RETURN;
        END IF;

        -- Add library_section_id column if it doesn't exist
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name = 'recommendations' AND column_name = 'library_section_id') THEN
            ALTER TABLE recommendations ADD COLUMN library_section_id INTEGER;
            RAISE NOTICE 'Added library_section_id column';
        END IF;
    END $$;
    """
    with get_db_cursor() as cursor:
        cursor.execute(migration_sql)

        # Populate from library_content for existing recommendations
        cursor.execute("""
            UPDATE recommendations r
            SET library_section_id = lc.library_section_id
            FROM library_content lc
            WHERE r.plex_rating_key = lc.plex_rating_key
            AND r.library_section_id IS NULL;
        """)

        # Add index for faster queries by user + library
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_recommendations_user_library
            ON recommendations(user_id, library_section_id)
            WHERE is_active = true;
        """)

    logger.info("migration_complete", migration="add_library_to_recommendations")
