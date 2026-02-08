"""Plex label management for recommendations."""

from plexapi.server import PlexServer

from plex_recommender.config import get_settings
from plex_recommender.db import get_db_cursor
from plex_recommender.logging import get_logger

logger = get_logger(__name__)


class PlexLabelService:
    """Service for managing AI recommendation labels on Plex content."""

    def __init__(self, url: str | None = None, token: str | None = None):
        settings = get_settings()
        self.plex_url = url or settings.plex_url
        self.plex_token = token or settings.plex_token
        self.label_prefix = settings.label_prefix
        self.min_confidence = settings.min_confidence_score
        self._server: PlexServer | None = None

    @property
    def server(self) -> PlexServer:
        """Lazy-load the Plex server connection."""
        if self._server is None:
            self._server = PlexServer(self.plex_url, self.plex_token)
        return self._server

    def _get_label_name(self, user_id: int, confidence: float) -> str:
        """Generate a label name based on user and confidence level."""
        # Categorize confidence into tiers
        if confidence >= 0.85:
            tier = "High"
        elif confidence >= 0.70:
            tier = "Medium"
        else:
            tier = "Low"

        # Get username for personalized labels
        with get_db_cursor(commit=False) as cursor:
            cursor.execute("SELECT username FROM users WHERE id = %s", (user_id,))
            row = cursor.fetchone()
            username = row["username"] if row else f"User{user_id}"

        return f"{self.label_prefix}:{username}:{tier}"

    def _get_item_by_rating_key(self, rating_key: str):
        """Get a Plex item by its rating key."""
        try:
            return self.server.fetchItem(int(rating_key))
        except Exception as e:
            logger.warning("item_fetch_failed", rating_key=rating_key, error=str(e))
            return None

    def apply_recommendation_labels(self, user_id: int) -> dict[str, int]:
        """Apply labels to all active recommendations for a user.

        Returns:
            Dict with counts of applied, skipped, and failed labels.
        """
        logger.info("applying_labels", user_id=user_id)
        results = {"applied": 0, "skipped": 0, "failed": 0}

        # Get active recommendations
        with get_db_cursor(commit=False) as cursor:
            cursor.execute(
                """
                SELECT id, plex_rating_key, confidence_score, title
                FROM recommendations
                WHERE user_id = %s
                    AND is_active = true
                    AND label_applied = false
                    AND confidence_score >= %s
                """,
                (user_id, self.min_confidence),
            )
            recommendations = cursor.fetchall()

        for rec in recommendations:
            rating_key = rec["plex_rating_key"]
            confidence = float(rec["confidence_score"])
            rec_id = rec["id"]

            try:
                item = self._get_item_by_rating_key(rating_key)
                if not item:
                    results["failed"] += 1
                    continue

                label_name = self._get_label_name(user_id, confidence)

                # Add the label
                item.addLabel(label_name)

                # Update database
                with get_db_cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE recommendations
                        SET label_applied = true, label_name = %s
                        WHERE id = %s
                        """,
                        (label_name, rec_id),
                    )

                results["applied"] += 1
                logger.debug("label_applied", title=rec["title"], label=label_name)

            except Exception as e:
                logger.error(
                    "label_application_failed",
                    rating_key=rating_key,
                    error=str(e),
                )
                results["failed"] += 1

        logger.info("labels_applied", user_id=user_id, results=results)
        return results

    def remove_stale_labels(self, user_id: int | None = None) -> int:
        """Remove labels from inactive/expired recommendations.

        Args:
            user_id: Optional user ID to scope cleanup. If None, cleans all.

        Returns:
            Number of labels removed.
        """
        logger.info("removing_stale_labels", user_id=user_id)
        removed = 0

        # Get inactive recommendations with labels
        with get_db_cursor(commit=False) as cursor:
            if user_id:
                cursor.execute(
                    """
                    SELECT id, plex_rating_key, label_name
                    FROM recommendations
                    WHERE user_id = %s
                        AND label_applied = true
                        AND (is_active = false OR expires_at < NOW())
                    """,
                    (user_id,),
                )
            else:
                cursor.execute(
                    """
                    SELECT id, plex_rating_key, label_name
                    FROM recommendations
                    WHERE label_applied = true
                        AND (is_active = false OR expires_at < NOW())
                    """
                )
            stale_recs = cursor.fetchall()

        for rec in stale_recs:
            rating_key = rec["plex_rating_key"]
            label_name = rec["label_name"]
            rec_id = rec["id"]

            if not label_name:
                continue

            try:
                item = self._get_item_by_rating_key(rating_key)
                if item:
                    item.removeLabel(label_name)

                # Update database
                with get_db_cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE recommendations
                        SET label_applied = false, label_name = NULL
                        WHERE id = %s
                        """,
                        (rec_id,),
                    )

                removed += 1
                logger.debug("label_removed", rating_key=rating_key, label=label_name)

            except Exception as e:
                logger.warning(
                    "label_removal_failed",
                    rating_key=rating_key,
                    error=str(e),
                )

        logger.info("stale_labels_removed", count=removed, user_id=user_id)
        return removed

    def cleanup_all_ai_labels(self, dry_run: bool = False) -> int:
        """Remove ALL AI recommendation labels from the library.

        This is a maintenance function for resetting labels.

        Args:
            dry_run: If True, only count labels without removing them.

        Returns:
            Number of labels found/removed.
        """
        logger.info("cleanup_all_ai_labels", dry_run=dry_run)
        count = 0

        for section in self.server.library.sections():
            if section.type not in ("movie", "show"):
                continue

            for item in section.all():
                labels = getattr(item, "labels", [])
                ai_labels = [lbl.tag for lbl in labels if lbl.tag.startswith(self.label_prefix)]

                for label in ai_labels:
                    count += 1
                    if not dry_run:
                        try:
                            item.removeLabel(label)
                            logger.debug(
                                "label_removed",
                                title=item.title,
                                label=label,
                            )
                        except Exception as e:
                            logger.warning(
                                "label_removal_failed",
                                title=item.title,
                                error=str(e),
                            )

        logger.info(
            "cleanup_complete",
            labels_found=count,
            dry_run=dry_run,
        )
        return count

    def apply_for_all_users(self) -> dict[int, dict[str, int]]:
        """Apply labels for all active users."""
        results = {}

        with get_db_cursor(commit=False) as cursor:
            cursor.execute("SELECT id FROM users WHERE is_active = true")
            user_ids = [row["id"] for row in cursor.fetchall()]

        for user_id in user_ids:
            try:
                results[user_id] = self.apply_recommendation_labels(user_id)
            except Exception as e:
                logger.error(
                    "user_label_application_failed",
                    user_id=user_id,
                    error=str(e),
                )
                results[user_id] = {"applied": 0, "skipped": 0, "failed": -1}

        return results

    def get_label_stats(self) -> dict:
        """Get statistics about current AI labels."""
        with get_db_cursor(commit=False) as cursor:
            cursor.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE label_applied = true) as labeled,
                    COUNT(*) FILTER (WHERE is_active = true) as active,
                    COUNT(*) FILTER (WHERE is_active = true AND label_applied = false) as pending,
                    COUNT(*) as total
                FROM recommendations
                """
            )
            row = cursor.fetchone()
            return dict(row) if row else {}
