"""User feedback service for tracking recommendation quality."""

from datetime import UTC, datetime
from typing import Any

from plexapi.server import PlexServer

from plex_recommender.config import get_settings
from plex_recommender.db import get_db_cursor
from plex_recommender.logging import get_logger

logger = get_logger(__name__)


# Feedback values
FEEDBACK_WATCHED = "watched"  # User watched but didn't complete
FEEDBACK_COMPLETED = "completed"  # User completed (>80% watched)
FEEDBACK_LOVED = "loved"  # User rated highly (8+/10 or thumbs up)
FEEDBACK_LIKED = "liked"  # User rated positively (6-7.9/10)
FEEDBACK_DISLIKED = "disliked"  # User rated negatively (<6/10 or thumbs down)
FEEDBACK_SKIPPED = "skipped"  # Recommendation expired without being watched

# Thresholds
COMPLETION_THRESHOLD = 80.0  # % watched to count as "completed"
LOVED_RATING_THRESHOLD = 8.0  # Rating out of 10 to count as "loved"
LIKED_RATING_THRESHOLD = 6.0  # Rating out of 10 to count as "liked"
SKIP_DAYS_THRESHOLD = 30  # Days before marking old recommendations as skipped


class FeedbackService:
    """Service for collecting and updating user feedback on recommendations."""

    def __init__(self, plex_url: str | None = None, plex_token: str | None = None):
        settings = get_settings()
        self.plex_url = plex_url or settings.plex_url
        self.plex_token = plex_token or settings.plex_token
        self._server: PlexServer | None = None

    @property
    def server(self) -> PlexServer:
        """Lazy-load the Plex server connection."""
        if self._server is None:
            self._server = PlexServer(self.plex_url, self.plex_token)
        return self._server

    def collect_feedback_for_user(self, user_id: int) -> dict[str, int]:
        """Collect feedback for all active recommendations for a user.

        Args:
            user_id: The internal user ID.

        Returns:
            Dict with counts of each feedback type applied.
        """
        logger.info("collecting_feedback", user_id=user_id)

        # Get all active recommendations for this user that don't have feedback yet
        recommendations = self._get_pending_recommendations(user_id)
        if not recommendations:
            logger.info("no_pending_recommendations", user_id=user_id)
            return {}

        # Get the user's Plex account token for user-specific ratings
        plex_user_id = self._get_plex_user_id(user_id)
        if not plex_user_id:
            logger.warning("plex_user_id_not_found", user_id=user_id)
            return {}

        # Get watch stats for this user
        watch_stats = self._get_watch_stats(user_id)

        # Get user ratings from Plex (if accessible)
        user_ratings = self._get_plex_user_ratings(plex_user_id, recommendations)

        # Process each recommendation
        feedback_counts: dict[str, int] = {}
        updates: list[tuple[str, int, str]] = []

        for rec in recommendations:
            rating_key = rec["plex_rating_key"]
            generated_at = rec["generated_at"]

            feedback = self._determine_feedback(
                rating_key=rating_key,
                generated_at=generated_at,
                watch_stats=watch_stats,
                user_ratings=user_ratings,
            )

            if feedback:
                updates.append((feedback, rec["id"], rating_key))
                feedback_counts[feedback] = feedback_counts.get(feedback, 0) + 1

        # Batch update feedback
        if updates:
            self._update_feedback_batch(updates)
            logger.info(
                "feedback_collected",
                user_id=user_id,
                total=len(updates),
                breakdown=feedback_counts,
            )

        return feedback_counts

    def collect_feedback_all_users(self) -> dict[int, dict[str, int]]:
        """Collect feedback for all users with recommendations needing feedback.

        Returns:
            Dict mapping user_id to their feedback counts.
        """
        logger.info("collecting_feedback_all_users")

        with get_db_cursor(commit=False) as cursor:
            cursor.execute(
                """
                SELECT DISTINCT user_id FROM recommendations
                WHERE user_feedback IS NULL
                """
            )
            user_ids = [row["user_id"] for row in cursor.fetchall()]

        results = {}
        for user_id in user_ids:
            try:
                feedback = self.collect_feedback_for_user(user_id)
                if feedback:
                    results[user_id] = feedback
            except Exception as e:
                logger.error("feedback_collection_failed", user_id=user_id, error=str(e))

        return results

    def _get_pending_recommendations(self, user_id: int) -> list[dict[str, Any]]:
        """Get recommendations that need feedback collection.

        Includes both active and inactive recommendations that haven't
        received feedback yet. This allows us to collect feedback on
        recommendations that were deactivated (replaced by newer ones)
        but may still have been watched/rated by the user.
        """
        with get_db_cursor(commit=False) as cursor:
            cursor.execute(
                """
                SELECT id, plex_rating_key, generated_at, is_active
                FROM recommendations
                WHERE user_id = %s
                  AND user_feedback IS NULL
                ORDER BY generated_at DESC
                """,
                (user_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def _get_plex_user_id(self, user_id: int) -> str | None:
        """Get the Plex user ID from our internal user ID."""
        with get_db_cursor(commit=False) as cursor:
            cursor.execute(
                "SELECT plex_user_id FROM users WHERE id = %s",
                (user_id,),
            )
            row = cursor.fetchone()
            return row["plex_user_id"] if row else None

    def _get_watch_stats(self, user_id: int) -> dict[str, dict[str, Any]]:
        """Get watch stats for a user, keyed by rating_key."""
        with get_db_cursor(commit=False) as cursor:
            cursor.execute(
                """
                SELECT plex_rating_key, total_play_count, avg_completion_percent, last_watched_at
                FROM watch_stats
                WHERE user_id = %s
                """,
                (user_id,),
            )
            return {row["plex_rating_key"]: dict(row) for row in cursor.fetchall()}

    def _get_plex_user_ratings(
        self,
        plex_user_id: str,
        recommendations: list[dict[str, Any]],
    ) -> dict[str, float]:
        """Get user ratings from Plex for the recommended items.

        Note: This requires the user to have rated items in Plex.
        User ratings are stored per-user and accessed via the Plex API.
        """
        ratings: dict[str, float] = {}

        # Get rating keys to look up
        rating_keys = [rec["plex_rating_key"] for rec in recommendations]
        if not rating_keys:
            return ratings

        try:
            # For server admin, we can access items directly
            # User-specific ratings require switching user context
            # For now, we'll get ratings from the admin perspective
            # TODO: Support per-user rating access with managed user tokens

            for rating_key in rating_keys:
                try:
                    item = self.server.fetchItem(int(rating_key))
                    if item and hasattr(item, "userRating") and item.userRating:
                        # Plex stores userRating as 0-10
                        ratings[rating_key] = float(item.userRating)
                except Exception:
                    # Item might not exist or be inaccessible
                    pass

        except Exception as e:
            logger.warning("plex_ratings_fetch_failed", error=str(e))

        return ratings

    def _determine_feedback(
        self,
        rating_key: str,
        generated_at: datetime,
        watch_stats: dict[str, dict[str, Any]],
        user_ratings: dict[str, float],
    ) -> str | None:
        """Determine the appropriate feedback for a recommendation.

        Priority:
        1. User rating (loved/liked/disliked) - explicit feedback
        2. Watch completion (completed/watched) - implicit feedback
        3. Skipped (old recommendation never watched)
        """
        # Check for explicit user rating first
        if rating_key in user_ratings:
            rating = user_ratings[rating_key]
            if rating >= LOVED_RATING_THRESHOLD:
                return FEEDBACK_LOVED
            elif rating >= LIKED_RATING_THRESHOLD:
                return FEEDBACK_LIKED
            else:
                return FEEDBACK_DISLIKED

        # Check for watch activity
        if rating_key in watch_stats:
            stats = watch_stats[rating_key]
            completion = stats.get("avg_completion_percent", 0) or 0

            if completion >= COMPLETION_THRESHOLD:
                return FEEDBACK_COMPLETED
            else:
                return FEEDBACK_WATCHED

        # Check if recommendation is old enough to mark as skipped
        if generated_at:
            # Handle timezone-aware vs naive datetimes
            now = datetime.now(UTC)
            if generated_at.tzinfo is None:
                generated_at = generated_at.replace(tzinfo=UTC)

            age_days = (now - generated_at).days
            if age_days >= SKIP_DAYS_THRESHOLD:
                return FEEDBACK_SKIPPED

        # No feedback determined yet (recommendation is recent and unwatched)
        return None

    def _update_feedback_batch(self, updates: list[tuple[str, int, str]]) -> None:
        """Batch update feedback for recommendations.

        Args:
            updates: List of (feedback, recommendation_id, rating_key) tuples.
        """
        with get_db_cursor() as cursor:
            for feedback, rec_id, rating_key in updates:
                cursor.execute(
                    """
                    UPDATE recommendations
                    SET user_feedback = %s, feedback_at = NOW()
                    WHERE id = %s
                    """,
                    (feedback, rec_id),
                )
                logger.debug(
                    "feedback_updated",
                    recommendation_id=rec_id,
                    rating_key=rating_key,
                    feedback=feedback,
                )

    def get_feedback_stats(self, user_id: int | None = None) -> dict[str, Any]:
        """Get feedback statistics.

        Args:
            user_id: Optional user ID to filter by.

        Returns:
            Dict with feedback statistics.
        """
        with get_db_cursor(commit=False) as cursor:
            if user_id:
                cursor.execute(
                    """
                    SELECT
                        user_feedback,
                        COUNT(*) as count
                    FROM recommendations
                    WHERE user_id = %s AND user_feedback IS NOT NULL
                    GROUP BY user_feedback
                    ORDER BY count DESC
                    """,
                    (user_id,),
                )
            else:
                cursor.execute(
                    """
                    SELECT
                        user_feedback,
                        COUNT(*) as count
                    FROM recommendations
                    WHERE user_feedback IS NOT NULL
                    GROUP BY user_feedback
                    ORDER BY count DESC
                    """
                )

            feedback_counts = {row["user_feedback"]: row["count"] for row in cursor.fetchall()}

            # Get totals
            if user_id:
                cursor.execute(
                    """
                    SELECT
                        COUNT(*) as total_recommendations,
                        COUNT(user_feedback) as with_feedback,
                        COUNT(*) FILTER (WHERE is_active) as active
                    FROM recommendations
                    WHERE user_id = %s
                    """,
                    (user_id,),
                )
            else:
                cursor.execute(
                    """
                    SELECT
                        COUNT(*) as total_recommendations,
                        COUNT(user_feedback) as with_feedback,
                        COUNT(*) FILTER (WHERE is_active) as active
                    FROM recommendations
                    """
                )

            totals = dict(cursor.fetchone())

        # Calculate quality metrics
        positive = (
            feedback_counts.get(FEEDBACK_LOVED, 0)
            + feedback_counts.get(FEEDBACK_LIKED, 0)
            + feedback_counts.get(FEEDBACK_COMPLETED, 0)
        )
        negative = feedback_counts.get(FEEDBACK_DISLIKED, 0) + feedback_counts.get(
            FEEDBACK_SKIPPED, 0
        )
        neutral = feedback_counts.get(FEEDBACK_WATCHED, 0)

        total_with_feedback = positive + negative + neutral
        hit_rate = (positive / total_with_feedback * 100) if total_with_feedback > 0 else 0

        return {
            "feedback_counts": feedback_counts,
            "totals": totals,
            "quality": {
                "positive": positive,
                "negative": negative,
                "neutral": neutral,
                "hit_rate_percent": round(hit_rate, 1),
            },
        }


def get_feedback_for_prompt(user_id: int, limit: int = 20) -> dict[str, list[dict]]:
    """Get feedback history formatted for inclusion in LLM prompts.

    Args:
        user_id: The user's internal ID.
        limit: Max items per feedback category.

    Returns:
        Dict with 'loved', 'liked', 'disliked', 'skipped' lists containing
        item metadata (title, genres, etc.) for prompt context.
    """
    with get_db_cursor(commit=False) as cursor:
        # Get recommendations with feedback, joined to library_content for metadata
        cursor.execute(
            """
            SELECT
                r.user_feedback,
                r.title,
                r.confidence_score,
                lc.genres,
                lc.keywords,
                lc.actors,
                lc.year
            FROM recommendations r
            LEFT JOIN library_content lc ON r.plex_rating_key = lc.plex_rating_key
            WHERE r.user_id = %s
              AND r.user_feedback IS NOT NULL
            ORDER BY r.feedback_at DESC
            """,
            (user_id,),
        )
        rows = cursor.fetchall()

    # Group by feedback type
    feedback: dict[str, list[dict]] = {
        FEEDBACK_LOVED: [],
        FEEDBACK_LIKED: [],
        FEEDBACK_DISLIKED: [],
        FEEDBACK_SKIPPED: [],
    }

    for row in rows:
        fb_type = row["user_feedback"]
        if fb_type in feedback and len(feedback[fb_type]) < limit:
            feedback[fb_type].append(
                {
                    "title": row["title"],
                    "genres": row["genres"] or [],
                    "keywords": row["keywords"] or [],
                    "actors": row["actors"] or [],
                    "year": row["year"],
                }
            )

    return feedback
