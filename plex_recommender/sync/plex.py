"""Plex library sync service."""

import json

from plexapi.server import PlexServer
from psycopg2.extras import execute_values
from tenacity import retry, stop_after_attempt, wait_exponential

from plex_recommender.config import get_settings
from plex_recommender.db import get_db_cursor
from plex_recommender.logging import get_logger

logger = get_logger(__name__)

# Batch size for database inserts
BATCH_SIZE = 500


class PlexSyncService:
    """Service for syncing library content from Plex to PostgreSQL."""

    def __init__(self, url: str | None = None, token: str | None = None):
        settings = get_settings()
        self.plex_url = url or settings.plex_url
        self.plex_token = token or settings.plex_token
        self._server: PlexServer | None = None

    @property
    def server(self) -> PlexServer:
        """Lazy-load the Plex server connection."""
        if self._server is None:
            self._server = PlexServer(self.plex_url, self.plex_token)
        return self._server

    def _update_sync_state(
        self,
        status: str,
        records_synced: int = 0,
        error: str | None = None,
    ) -> None:
        """Update the sync state in the database."""
        with get_db_cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO sync_state (sync_type, status, records_synced, error_message, last_sync_at)
                VALUES ('plex_library', %s, %s, %s, NOW())
                ON CONFLICT (sync_type) DO UPDATE SET
                    status = EXCLUDED.status,
                    records_synced = EXCLUDED.records_synced,
                    error_message = EXCLUDED.error_message,
                    last_sync_at = NOW()
                """,
                (status, records_synced, error),
            )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def sync_library(self, library_name: str | None = None) -> int:
        """Sync library content from Plex.

        Args:
            library_name: Optional specific library to sync. If None, syncs all.
        """
        logger.info("sync_library_started", library=library_name)
        self._update_sync_state("running")

        try:
            total_synced = 0
            sections = self.server.library.sections()

            # First, sync library section metadata
            self._sync_library_sections(sections)

            for section in sections:
                # Skip non-video libraries
                if section.type not in ("movie", "show"):
                    continue

                if library_name and section.title != library_name:
                    continue

                logger.info("syncing_section", section=section.title, type=section.type)
                synced = self._sync_section(section)
                total_synced += synced

            self._update_sync_state("idle", total_synced)
            logger.info("sync_library_completed", total_synced=total_synced)
            return total_synced

        except Exception as e:
            logger.error("sync_library_failed", error=str(e))
            self._update_sync_state("failed", error=str(e))
            raise

    def _sync_library_sections(self, sections) -> None:
        """Sync library section metadata."""
        logger.info("syncing_library_sections")
        with get_db_cursor() as cursor:
            for section in sections:
                cursor.execute(
                    """
                    INSERT INTO library_sections (section_id, name, section_type, agent, scanner, thumb_url)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (section_id) DO UPDATE SET
                        name = EXCLUDED.name,
                        section_type = EXCLUDED.section_type,
                        agent = EXCLUDED.agent,
                        scanner = EXCLUDED.scanner,
                        thumb_url = EXCLUDED.thumb_url,
                        updated_at = NOW()
                    """,
                    (
                        int(section.key),
                        section.title,
                        section.type,
                        getattr(section, "agent", None),
                        getattr(section, "scanner", None),
                        getattr(section, "thumb", None),
                    ),
                )
        logger.info("library_sections_synced", count=len(sections))

    def _sync_section(self, section) -> int:
        """Sync a single library section using batch inserts."""
        synced = 0
        batch = []

        items = section.all()
        content_type = "movie" if section.type == "movie" else "show"

        for item in items:
            record = self._extract_content_record(item, section.key, content_type)
            if record:
                batch.append(record)
                synced += 1

                # Flush batch when it reaches BATCH_SIZE
                if len(batch) >= BATCH_SIZE:
                    self._batch_upsert_content(batch)
                    logger.info("sync_progress", section=section.title, synced=synced)
                    batch = []

        # Flush remaining items
        if batch:
            self._batch_upsert_content(batch)
            logger.info("sync_progress", section=section.title, synced=synced)

        return synced

    def _extract_content_record(
        self,
        item,
        section_key: str,
        content_type: str,
        parent_key: str | None = None,
    ) -> tuple | None:
        """Extract content data from a Plex item into a tuple for batch insert."""
        try:
            # Extract genres
            genres = []
            if hasattr(item, "genres") and item.genres:
                genres = [g.tag for g in item.genres]

            # Extract actors (top 10)
            actors = []
            if hasattr(item, "roles") and item.roles:
                actors = [a.tag for a in item.roles[:10]]

            # Keywords are populated from TMDB via `plex-rec sync tmdb`
            # Plex labels are user-defined tags, not content keywords
            keywords = None

            # Extract languages from media streams (simplified for speed)
            languages = []
            try:
                if hasattr(item, "media") and item.media and len(item.media) > 0:
                    media = item.media[0]
                    if hasattr(media, "parts") and media.parts and len(media.parts) > 0:
                        part = media.parts[0]
                        if hasattr(part, "streams") and part.streams:
                            for stream in part.streams:
                                if stream.streamType == 2:  # Audio stream
                                    lang = getattr(stream, "language", None) or getattr(
                                        stream, "languageCode", None
                                    )
                                    if lang and lang not in languages:
                                        languages.append(lang)
                                        if len(languages) >= 5:  # Limit to 5 languages
                                            break
            except Exception:
                pass  # Language extraction is best-effort

            # Build metadata JSON for flexible storage
            metadata = {
                "guids": [g.id for g in getattr(item, "guids", [])]
                if hasattr(item, "guids")
                else [],
                "directors": [d.tag for d in getattr(item, "directors", [])]
                if hasattr(item, "directors")
                else [],
                "writers": [w.tag for w in getattr(item, "writers", [])]
                if hasattr(item, "writers")
                else [],
                "collections": [c.tag for c in getattr(item, "collections", [])]
                if hasattr(item, "collections")
                else [],
            }

            # Handle dates
            added_at = getattr(item, "addedAt", None)
            originally_available = getattr(item, "originallyAvailableAt", None)

            # Get parent/grandparent keys for episodes
            grandparent_key = None
            if content_type == "episode":
                parent_key = str(getattr(item, "parentRatingKey", "")) or parent_key
                grandparent_key = str(getattr(item, "grandparentRatingKey", ""))

            return (
                str(item.ratingKey),
                int(section_key),
                content_type,
                item.title,
                getattr(item, "originalTitle", None),
                getattr(item, "year", None),
                getattr(item, "summary", None),
                genres if genres else None,
                actors if actors else None,
                keywords if keywords else None,
                languages if languages else None,
                getattr(item, "studio", None),
                getattr(item, "contentRating", None),
                getattr(item, "rating", None),
                getattr(item, "audienceRating", None),
                getattr(item, "duration", None),
                item.thumbUrl if hasattr(item, "thumbUrl") else None,
                getattr(item, "artUrl", None),
                added_at,
                originally_available,
                parent_key,
                grandparent_key,
                json.dumps(metadata),
            )
        except Exception as e:
            logger.warning(
                "content_extract_failed",
                rating_key=getattr(item, "ratingKey", "unknown"),
                error=str(e),
            )
            return None

    def _batch_upsert_content(self, records: list[tuple]) -> None:
        """Batch upsert content records using execute_values for speed."""
        if not records:
            return

        with get_db_cursor() as cursor:
            execute_values(
                cursor,
                """
                INSERT INTO library_content (
                    plex_rating_key, library_section_id, content_type,
                    title, original_title, year, summary, genres,
                    actors, keywords, languages,
                    studio, content_rating, rating, audience_rating,
                    duration_ms, thumb_url, art_url, added_at,
                    originally_available_at, parent_rating_key,
                    grandparent_rating_key, metadata_json
                )
                VALUES %s
                ON CONFLICT (plex_rating_key) DO UPDATE SET
                    title = EXCLUDED.title,
                    original_title = EXCLUDED.original_title,
                    year = EXCLUDED.year,
                    summary = EXCLUDED.summary,
                    genres = EXCLUDED.genres,
                    actors = EXCLUDED.actors,
                    keywords = EXCLUDED.keywords,
                    languages = EXCLUDED.languages,
                    studio = EXCLUDED.studio,
                    content_rating = EXCLUDED.content_rating,
                    rating = EXCLUDED.rating,
                    audience_rating = EXCLUDED.audience_rating,
                    duration_ms = EXCLUDED.duration_ms,
                    thumb_url = EXCLUDED.thumb_url,
                    art_url = EXCLUDED.art_url,
                    metadata_json = EXCLUDED.metadata_json,
                    updated_at = NOW()
                """,
                records,
                page_size=BATCH_SIZE,
            )

    def get_unwatched_content(self, user_id: int, content_type: str = "movie") -> list[dict]:
        """Get unwatched content for a user from the library."""
        with get_db_cursor(commit=False) as cursor:
            cursor.execute(
                """
                SELECT lc.*
                FROM library_content lc
                LEFT JOIN watch_stats ws ON
                    lc.plex_rating_key = ws.plex_rating_key
                    AND ws.user_id = %s
                WHERE lc.content_type = %s
                    AND ws.id IS NULL
                ORDER BY lc.rating DESC NULLS LAST, lc.added_at DESC
                """,
                (user_id, content_type),
            )
            return cursor.fetchall()

    def get_content_by_genres(
        self,
        genres: list[str],
        content_type: str = "movie",
        limit: int = 100,
    ) -> list[dict]:
        """Get content matching specified genres."""
        with get_db_cursor(commit=False) as cursor:
            cursor.execute(
                """
                SELECT lc.*,
                       array_length(array(SELECT unnest(genres) INTERSECT SELECT unnest(%s::text[])), 1) as genre_match_count
                FROM library_content lc
                WHERE lc.content_type = %s
                    AND lc.genres && %s
                ORDER BY genre_match_count DESC NULLS LAST, lc.rating DESC NULLS LAST
                LIMIT %s
                """,
                (genres, content_type, genres, limit),
            )
            return cursor.fetchall()
