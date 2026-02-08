"""Service for managing library content embeddings."""

from typing import Any

from plex_recommender.db import get_db_cursor
from plex_recommender.embeddings.store import VectorStore
from plex_recommender.logging import get_logger

logger = get_logger(__name__)


class EmbeddingsService:
    """Service for generating and managing library content embeddings.

    This service coordinates between the PostgreSQL database (library_content)
    and the LanceDB vector store, keeping embeddings in sync with library updates.
    """

    def __init__(self, vector_store: VectorStore | None = None):
        """Initialize the embeddings service.

        Args:
            vector_store: Optional VectorStore instance. Creates one if not provided.
        """
        self.vector_store = vector_store or VectorStore()

    def get_library_content(
        self,
        library_section_id: int | None = None,
        content_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch library content from PostgreSQL.

        Args:
            library_section_id: Optional filter by library section.
            content_types: Optional filter by content types (e.g., ['movie', 'show']).

        Returns:
            List of content dicts with all metadata fields.
        """
        query = """
            SELECT
                plex_rating_key,
                library_section_id,
                content_type,
                title,
                year,
                summary,
                genres,
                actors,
                keywords,
                languages,
                studio,
                content_rating,
                rating,
                audience_rating,
                metadata_json
            FROM library_content
            WHERE 1=1
        """
        params: list[Any] = []

        if library_section_id is not None:
            query += " AND library_section_id = %s"
            params.append(library_section_id)

        if content_types:
            placeholders = ", ".join(["%s"] * len(content_types))
            query += f" AND content_type IN ({placeholders})"
            params.extend(content_types)

        query += " ORDER BY library_section_id, title"

        with get_db_cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

        # RealDictRow objects already behave like dicts, just convert them
        return [dict(row) for row in rows]

    def generate_embeddings(
        self,
        library_section_id: int | None = None,
        content_types: list[str] | None = None,
        batch_size: int = 50,
        progress_callback: Any | None = None,
    ) -> dict[str, Any]:
        """Generate embeddings for library content.

        Args:
            library_section_id: Optional filter by library section.
            content_types: Optional filter by content types.
            batch_size: Number of items to process per batch.
            progress_callback: Optional callback(current, total) for progress.

        Returns:
            Dict with generation statistics.
        """
        # Filter to movies and shows by default (skip episodes/seasons)
        if content_types is None:
            content_types = ["movie", "show"]

        # Fetch content from database
        content = self.get_library_content(
            library_section_id=library_section_id,
            content_types=content_types,
        )

        if not content:
            logger.warning("no_content_to_embed")
            return {
                "status": "no_content",
                "total": 0,
                "processed": 0,
            }

        logger.info(
            "starting_embedding_generation",
            total=len(content),
            library_section_id=library_section_id,
        )

        # Generate embeddings
        processed = self.vector_store.add_content(
            content_list=content,
            batch_size=batch_size,
            progress_callback=progress_callback,
        )

        return {
            "status": "success",
            "total": len(content),
            "processed": processed,
        }

    def search_for_user(
        self,
        user_id: int,
        library_section_id: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Search for content relevant to a user based on their watch history.

        Uses the user's FULL watch history across all libraries to find similar
        content, but filters results to the target library. This allows cross-library
        discovery (e.g., if you like sci-fi movies, find similar sci-fi shows).

        Args:
            user_id: The user's database ID.
            library_section_id: Filter RESULTS to this library (not watch history).
            limit: Maximum number of results.

        Returns:
            List of relevant content dicts with full metadata.
        """
        from plex_recommender.config import get_settings

        settings = get_settings()
        limit = limit or settings.rag_top_k

        # Get user's FULL watch history (not filtered by library)
        # This enables cross-library discovery
        watched_content = self._get_user_watched_content(user_id, library_section_id=None)

        if not watched_content:
            logger.warning("no_watch_history_for_user", user_id=user_id)
            return []

        # Search vector store for similar content, filtered to target library
        similar = self.vector_store.search_by_watch_history(
            watched_content=watched_content,
            limit=limit,
            library_section_id=library_section_id,  # Filter results, not input
        )

        # Fetch full content metadata for results
        if similar:
            rating_keys = [r["plex_rating_key"] for r in similar]
            return self._fetch_content_by_keys(rating_keys)

        return []

    def _get_user_watched_content(
        self,
        user_id: int,
        library_section_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Get user's watched content with metadata for embedding.

        Args:
            user_id: The user's database ID.
            library_section_id: Optional filter by library section.

        Returns:
            List of watched content with metadata.
        """
        query = """
            SELECT
                lc.plex_rating_key,
                lc.library_section_id,
                lc.content_type,
                lc.title,
                lc.year,
                lc.summary,
                lc.genres,
                lc.actors,
                lc.keywords,
                lc.languages,
                lc.studio,
                ws.total_play_count as play_count,
                ws.avg_completion_percent as avg_completion,
                ws.last_watched_at
            FROM watch_stats ws
            JOIN library_content lc ON ws.plex_rating_key = lc.plex_rating_key
            WHERE ws.user_id = %s
        """
        params: list[Any] = [user_id]

        if library_section_id is not None:
            query += " AND lc.library_section_id = %s"
            params.append(library_section_id)

        query += " ORDER BY ws.last_watched_at DESC"

        with get_db_cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

        # RealDictRow objects already behave like dicts
        return [dict(row) for row in rows]

    def _fetch_content_by_keys(
        self,
        rating_keys: list[str],
    ) -> list[dict[str, Any]]:
        """Fetch full content metadata for given rating keys.

        Args:
            rating_keys: List of plex_rating_key values.

        Returns:
            List of content dicts in the same order as rating_keys.
        """
        if not rating_keys:
            return []

        placeholders = ", ".join(["%s"] * len(rating_keys))
        query = f"""
            SELECT
                plex_rating_key,
                library_section_id,
                content_type,
                title,
                year,
                summary,
                genres,
                actors,
                keywords,
                languages,
                studio,
                content_rating,
                rating,
                audience_rating,
                metadata_json
            FROM library_content
            WHERE plex_rating_key IN ({placeholders})
        """

        with get_db_cursor() as cur:
            cur.execute(query, rating_keys)
            rows = cur.fetchall()

        # Create dict for ordering - RealDictRow objects behave like dicts
        content_map = {row["plex_rating_key"]: dict(row) for row in rows}

        # Return in original order
        return [content_map[key] for key in rating_keys if key in content_map]

    def get_stats(self) -> dict[str, Any]:
        """Get embedding statistics.

        Returns:
            Dict with embedding counts and metadata.
        """
        vs_stats = self.vector_store.get_stats()

        # Get library content count from database
        with get_db_cursor() as cur:
            cur.execute("""
                SELECT content_type, COUNT(*) as count
                FROM library_content
                WHERE content_type IN ('movie', 'show')
                GROUP BY content_type
            """)
            rows = cur.fetchall()
            db_counts = {row["content_type"]: row["count"] for row in rows}

        total_embeddable = sum(db_counts.values()) if db_counts else 0

        return {
            "vector_store": vs_stats,
            "database": {
                "total_embeddable": total_embeddable,
                "by_type": db_counts,
            },
            "coverage": (
                vs_stats.get("total_embeddings", 0) / total_embeddable * 100
                if total_embeddable > 0
                else 0
            ),
        }

    def close(self):
        """Clean up resources."""
        self.vector_store.close()
