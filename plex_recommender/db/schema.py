"""Database schema definitions and migrations."""

from plex_recommender.db import get_db_cursor
from plex_recommender.logging import get_logger

logger = get_logger(__name__)

SCHEMA_SQL = """
-- Users table (synced from Plex/Tautulli)
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    plex_user_id VARCHAR(255) UNIQUE NOT NULL,
    username VARCHAR(255) NOT NULL,
    email VARCHAR(255),
    thumb_url TEXT,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Library sections (Plex libraries)
CREATE TABLE IF NOT EXISTS library_sections (
    id SERIAL PRIMARY KEY,
    section_id INTEGER UNIQUE NOT NULL,
    name VARCHAR(255) NOT NULL,
    section_type VARCHAR(50) NOT NULL,  -- 'movie', 'show', 'artist', etc.
    agent VARCHAR(255),
    scanner VARCHAR(255),
    thumb_url TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Library content cache (Plex metadata)
CREATE TABLE IF NOT EXISTS library_content (
    id SERIAL PRIMARY KEY,
    plex_rating_key VARCHAR(255) UNIQUE NOT NULL,
    library_section_id INTEGER NOT NULL,
    content_type VARCHAR(50) NOT NULL,  -- 'movie', 'show', 'episode'
    title VARCHAR(500) NOT NULL,
    original_title VARCHAR(500),
    year INTEGER,
    summary TEXT,
    genres TEXT[],  -- Array of genre names
    actors TEXT[],  -- Array of actor names (top 10)
    keywords TEXT[],  -- Array of keywords/tags from TMDB
    languages TEXT[],  -- Array of audio languages
    studio VARCHAR(255),
    content_rating VARCHAR(50),
    rating DECIMAL(3, 1),
    audience_rating DECIMAL(3, 1),
    duration_ms INTEGER,
    thumb_url TEXT,
    art_url TEXT,
    added_at TIMESTAMP WITH TIME ZONE,
    originally_available_at DATE,
    parent_rating_key VARCHAR(255),  -- For episodes: show's rating key
    grandparent_rating_key VARCHAR(255),  -- For episodes: season's rating key
    metadata_json JSONB,  -- Full metadata for flexibility
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Watch history (synced from Tautulli)
CREATE TABLE IF NOT EXISTS watch_history (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    plex_rating_key VARCHAR(255) NOT NULL,
    session_key VARCHAR(255),
    content_type VARCHAR(50) NOT NULL,
    title VARCHAR(500) NOT NULL,
    parent_title VARCHAR(500),  -- Show name for episodes
    grandparent_title VARCHAR(500),  -- Season name for episodes
    watched_at TIMESTAMP WITH TIME ZONE NOT NULL,
    watch_duration_seconds INTEGER,
    total_duration_seconds INTEGER,
    percent_complete DECIMAL(5, 2),
    play_count INTEGER DEFAULT 1,
    platform VARCHAR(100),
    player VARCHAR(255),
    ip_address VARCHAR(45),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(user_id, plex_rating_key, watched_at)
);

-- Aggregated watch statistics per user/content
CREATE TABLE IF NOT EXISTS watch_stats (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    plex_rating_key VARCHAR(255) NOT NULL,
    content_type VARCHAR(50) NOT NULL,
    total_play_count INTEGER DEFAULT 0,
    total_watch_time_seconds INTEGER DEFAULT 0,
    avg_completion_percent DECIMAL(5, 2),
    last_watched_at TIMESTAMP WITH TIME ZONE,
    first_watched_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(user_id, plex_rating_key)
);

-- Genre preferences per user (computed from watch history)
CREATE TABLE IF NOT EXISTS user_genre_preferences (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    genre VARCHAR(100) NOT NULL,
    watch_count INTEGER DEFAULT 0,
    total_watch_time_seconds INTEGER DEFAULT 0,
    avg_completion_percent DECIMAL(5, 2),
    affinity_score DECIMAL(5, 4),  -- Normalized 0-1 preference score
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(user_id, genre)
);

-- AI-generated recommendations
CREATE TABLE IF NOT EXISTS recommendations (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    plex_rating_key VARCHAR(255) NOT NULL,
    content_type VARCHAR(50) NOT NULL,
    title VARCHAR(500) NOT NULL,
    confidence_score DECIMAL(3, 2) NOT NULL,  -- 0.00 to 1.00
    reasoning TEXT,  -- LLM explanation for the recommendation
    recommendation_factors JSONB,  -- Structured factors that led to rec
    model_used VARCHAR(100),
    prompt_hash VARCHAR(64),  -- Hash of prompt for reproducibility
    label_applied BOOLEAN DEFAULT false,
    label_name VARCHAR(100),
    generated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    expires_at TIMESTAMP WITH TIME ZONE,
    is_active BOOLEAN DEFAULT true,
    user_feedback VARCHAR(20),  -- 'liked', 'disliked', 'watched', 'dismissed'
    feedback_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(user_id, plex_rating_key, generated_at)
);

-- Sync state tracking
CREATE TABLE IF NOT EXISTS sync_state (
    id SERIAL PRIMARY KEY,
    sync_type VARCHAR(50) UNIQUE NOT NULL,  -- 'tautulli_history', 'plex_library'
    last_sync_at TIMESTAMP WITH TIME ZONE,
    last_sync_cursor TEXT,  -- For pagination (e.g., last timestamp or ID)
    records_synced INTEGER DEFAULT 0,
    status VARCHAR(20) DEFAULT 'idle',  -- 'idle', 'running', 'failed'
    error_message TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_watch_history_user_id ON watch_history(user_id);
CREATE INDEX IF NOT EXISTS idx_watch_history_watched_at ON watch_history(watched_at);
CREATE INDEX IF NOT EXISTS idx_watch_history_rating_key ON watch_history(plex_rating_key);
CREATE INDEX IF NOT EXISTS idx_library_content_type ON library_content(content_type);
CREATE INDEX IF NOT EXISTS idx_library_genres ON library_content USING GIN(genres);
CREATE INDEX IF NOT EXISTS idx_recommendations_user_active ON recommendations(user_id, is_active);
CREATE INDEX IF NOT EXISTS idx_recommendations_label ON recommendations(label_applied, is_active);
CREATE INDEX IF NOT EXISTS idx_user_genre_pref ON user_genre_preferences(user_id, affinity_score DESC);

-- Function to update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Triggers for updated_at
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'update_users_updated_at') THEN
        CREATE TRIGGER update_users_updated_at BEFORE UPDATE ON users
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'update_library_content_updated_at') THEN
        CREATE TRIGGER update_library_content_updated_at BEFORE UPDATE ON library_content
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'update_watch_stats_updated_at') THEN
        CREATE TRIGGER update_watch_stats_updated_at BEFORE UPDATE ON watch_stats
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'update_user_genre_preferences_updated_at') THEN
        CREATE TRIGGER update_user_genre_preferences_updated_at BEFORE UPDATE ON user_genre_preferences
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'update_sync_state_updated_at') THEN
        CREATE TRIGGER update_sync_state_updated_at BEFORE UPDATE ON sync_state
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
    END IF;
END $$;
"""


def create_tables() -> None:
    """Create all database tables."""
    with get_db_cursor() as cursor:
        cursor.execute(SCHEMA_SQL)
        logger.info("database_schema_created")


def drop_tables() -> None:
    """Drop all database tables (use with caution!)."""
    drop_sql = """
    DROP TABLE IF EXISTS recommendations CASCADE;
    DROP TABLE IF EXISTS user_genre_preferences CASCADE;
    DROP TABLE IF EXISTS watch_stats CASCADE;
    DROP TABLE IF EXISTS watch_history CASCADE;
    DROP TABLE IF EXISTS library_content CASCADE;
    DROP TABLE IF EXISTS library_sections CASCADE;
    DROP TABLE IF EXISTS sync_state CASCADE;
    DROP TABLE IF EXISTS schema_migrations CASCADE;
    DROP TABLE IF EXISTS users CASCADE;
    DROP FUNCTION IF EXISTS update_updated_at_column CASCADE;
    """
    with get_db_cursor() as cursor:
        cursor.execute(drop_sql)
        logger.warning("database_tables_dropped")
