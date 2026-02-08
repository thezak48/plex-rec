"""Tautulli API client and watch history sync service."""

from datetime import UTC, datetime
from typing import Any

import httpx
from psycopg2.extras import execute_values
from tenacity import retry, stop_after_attempt, wait_exponential

from plex_recommender.config import get_settings
from plex_recommender.db import get_db_cursor
from plex_recommender.logging import get_logger

logger = get_logger(__name__)

# Batch size for API requests and DB inserts
BATCH_SIZE = 500


class TautulliClient:
    """Client for interacting with Tautulli API."""

    def __init__(self, url: str | None = None, api_key: str | None = None):
        settings = get_settings()
        self.base_url = (url or settings.tautulli_url).rstrip("/")
        self.api_key = api_key or settings.tautulli_api_key
        self._client = httpx.Client(timeout=30.0)

    def _make_request(self, cmd: str, **params) -> dict[str, Any]:
        """Make a request to the Tautulli API."""
        params = {k: v for k, v in params.items() if v is not None}
        params["apikey"] = self.api_key
        params["cmd"] = cmd

        response = self._client.get(f"{self.base_url}/api/v2", params=params)
        response.raise_for_status()

        data = response.json()
        if data.get("response", {}).get("result") != "success":
            raise ValueError(f"Tautulli API error: {data}")

        return data.get("response", {}).get("data", {})

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def get_history(
        self,
        length: int = 100,
        start: int = 0,
        user_id: str | None = None,
        after: datetime | None = None,
    ) -> dict[str, Any]:
        """Get watch history from Tautulli with pagination."""
        params = {
            "length": length,
            "start": start,
        }
        if user_id:
            params["user_id"] = user_id
        if after:
            params["after"] = after.strftime("%Y-%m-%d")

        return self._make_request("get_history", **params)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def get_users(self) -> list[dict[str, Any]]:
        """Get all users from Tautulli."""
        return self._make_request("get_users")

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def get_user(self, user_id: str) -> dict[str, Any]:
        """Get a specific user from Tautulli."""
        return self._make_request("get_user", user_id=user_id)

    def close(self):
        """Close the HTTP client."""
        self._client.close()


class TautulliSyncService:
    """Service for syncing watch history from Tautulli to PostgreSQL."""

    def __init__(self):
        self.client = TautulliClient()

    def sync_users(self) -> int:
        """Sync users from Tautulli to the database."""
        logger.info("sync_users_started")
        users = self.client.get_users()
        synced_count = 0

        with get_db_cursor() as cursor:
            for user in users:
                cursor.execute(
                    """
                    INSERT INTO users (plex_user_id, username, email, thumb_url)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (plex_user_id) DO UPDATE SET
                        username = EXCLUDED.username,
                        email = EXCLUDED.email,
                        thumb_url = EXCLUDED.thumb_url,
                        updated_at = NOW()
                    """,
                    (
                        str(user.get("user_id")),
                        user.get("friendly_name") or user.get("username"),
                        user.get("email"),
                        user.get("user_thumb"),
                    ),
                )
                synced_count += 1

        logger.info("sync_users_completed", count=synced_count)
        return synced_count

    def _get_last_sync_cursor(self) -> datetime | None:
        """Get the last sync timestamp for incremental sync."""
        with get_db_cursor(commit=False) as cursor:
            cursor.execute(
                """
                SELECT last_sync_cursor FROM sync_state
                WHERE sync_type = 'tautulli_history'
                """
            )
            row = cursor.fetchone()
            if row and row["last_sync_cursor"]:
                return datetime.fromisoformat(row["last_sync_cursor"])
        return None

    def _update_sync_state(
        self,
        status: str,
        cursor_value: str | None = None,
        records_synced: int = 0,
        error: str | None = None,
    ) -> None:
        """Update the sync state in the database."""
        with get_db_cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO sync_state (sync_type, status, last_sync_cursor, records_synced, error_message, last_sync_at)
                VALUES ('tautulli_history', %s, %s, %s, %s, NOW())
                ON CONFLICT (sync_type) DO UPDATE SET
                    status = EXCLUDED.status,
                    last_sync_cursor = COALESCE(EXCLUDED.last_sync_cursor, sync_state.last_sync_cursor),
                    records_synced = sync_state.records_synced + EXCLUDED.records_synced,
                    error_message = EXCLUDED.error_message,
                    last_sync_at = NOW()
                """,
                (status, cursor_value, records_synced, error),
            )

    def _get_user_id_map(self) -> dict[str, int]:
        """Get mapping of plex_user_id to internal user id."""
        with get_db_cursor(commit=False) as cursor:
            cursor.execute("SELECT id, plex_user_id FROM users")
            return {row["plex_user_id"]: row["id"] for row in cursor.fetchall()}

    def sync_history(self, full_sync: bool = False) -> int:
        """Sync watch history from Tautulli with incremental support."""
        logger.info("sync_history_started", full_sync=full_sync)
        self._update_sync_state("running")

        try:
            # First sync users to ensure we have them
            self.sync_users()
            user_id_map = self._get_user_id_map()

            # Determine start point for incremental sync
            after_date = None if full_sync else self._get_last_sync_cursor()
            if after_date:
                logger.info("incremental_sync", after=after_date.isoformat())

            total_synced = 0
            start = 0
            page_size = BATCH_SIZE
            latest_watched_at: datetime | None = None

            while True:
                history_data = self.client.get_history(
                    length=page_size,
                    start=start,
                    after=after_date,
                )

                records = history_data.get("data", [])
                if not records:
                    break

                synced = self._upsert_history_batch(records, user_id_map)
                total_synced += synced

                # Track the latest watched_at for cursor
                for record in records:
                    watched_ts = record.get("stopped") or record.get("started")
                    if watched_ts:
                        watched_at = datetime.fromtimestamp(watched_ts, tz=UTC)
                        if latest_watched_at is None or watched_at > latest_watched_at:
                            latest_watched_at = watched_at

                # Check if we've reached the end
                total_count = history_data.get("recordsFiltered", 0)
                start += page_size
                if start >= total_count:
                    break

                logger.info("sync_progress", synced=total_synced, total=total_count)

            # Update sync state with success
            cursor_value = latest_watched_at.isoformat() if latest_watched_at else None
            self._update_sync_state("idle", cursor_value, total_synced)

            # Update aggregated stats
            self._update_watch_stats()
            self._update_genre_preferences()

            logger.info("sync_history_completed", total_synced=total_synced)
            return total_synced

        except Exception as e:
            logger.error("sync_history_failed", error=str(e))
            self._update_sync_state("failed", error=str(e))
            raise

    def _upsert_history_batch(self, records: list[dict], user_id_map: dict[str, int]) -> int:
        """Upsert a batch of history records using execute_values for speed."""
        batch = []
        for record in records:
            plex_user_id = str(record.get("user_id"))
            user_id = user_id_map.get(plex_user_id)
            if not user_id:
                continue

            watched_ts = record.get("stopped") or record.get("started")
            if not watched_ts:
                continue

            watched_at = datetime.fromtimestamp(watched_ts, tz=UTC)
            duration = record.get("duration", 0)
            total_duration = record.get("media_info", {}).get("duration") or record.get(
                "full_duration", 0
            )
            percent = (duration / total_duration * 100) if total_duration > 0 else 0

            batch.append(
                (
                    user_id,
                    str(record.get("rating_key")),
                    record.get("session_key"),
                    record.get("media_type", "unknown"),
                    record.get("title"),
                    record.get("parent_title"),
                    record.get("grandparent_title"),
                    watched_at,
                    duration,
                    total_duration,
                    percent,
                    record.get("platform"),
                    record.get("player"),
                    record.get("ip_address"),
                )
            )

        if not batch:
            return 0

        # Deduplicate batch by (user_id, plex_rating_key, watched_at) - keep last occurrence
        # This prevents "ON CONFLICT DO UPDATE command cannot affect row a second time" error
        seen = {}
        for record in batch:
            key = (record[0], record[1], record[7])  # user_id, plex_rating_key, watched_at
            seen[key] = record
        batch = list(seen.values())

        with get_db_cursor() as cursor:
            execute_values(
                cursor,
                """
                INSERT INTO watch_history (
                    user_id, plex_rating_key, session_key, content_type,
                    title, parent_title, grandparent_title, watched_at,
                    watch_duration_seconds, total_duration_seconds, percent_complete,
                    platform, player, ip_address
                )
                VALUES %s
                ON CONFLICT (user_id, plex_rating_key, watched_at) DO UPDATE SET
                    watch_duration_seconds = GREATEST(watch_history.watch_duration_seconds, EXCLUDED.watch_duration_seconds),
                    percent_complete = GREATEST(watch_history.percent_complete, EXCLUDED.percent_complete),
                    play_count = watch_history.play_count + 1
                """,
                batch,
                page_size=BATCH_SIZE,
            )

        return len(batch)

    def _update_watch_stats(self) -> None:
        """Update aggregated watch statistics per user/content."""
        logger.info("updating_watch_stats")
        with get_db_cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO watch_stats (
                    user_id, plex_rating_key, content_type,
                    total_play_count, total_watch_time_seconds,
                    avg_completion_percent, last_watched_at, first_watched_at
                )
                SELECT
                    user_id,
                    plex_rating_key,
                    content_type,
                    SUM(play_count),
                    SUM(watch_duration_seconds),
                    AVG(percent_complete),
                    MAX(watched_at),
                    MIN(watched_at)
                FROM watch_history
                GROUP BY user_id, plex_rating_key, content_type
                ON CONFLICT (user_id, plex_rating_key) DO UPDATE SET
                    total_play_count = EXCLUDED.total_play_count,
                    total_watch_time_seconds = EXCLUDED.total_watch_time_seconds,
                    avg_completion_percent = EXCLUDED.avg_completion_percent,
                    last_watched_at = EXCLUDED.last_watched_at,
                    first_watched_at = EXCLUDED.first_watched_at,
                    updated_at = NOW()
                """
            )

    def _update_genre_preferences(self) -> None:
        """Update user genre preferences based on watch history."""
        logger.info("updating_genre_preferences")
        with get_db_cursor() as cursor:
            # Calculate genre preferences from watch history joined with library content
            cursor.execute(
                """
                WITH genre_stats AS (
                    SELECT
                        ws.user_id,
                        unnest(lc.genres) as genre,
                        COUNT(*) as watch_count,
                        SUM(ws.total_watch_time_seconds) as total_time,
                        AVG(ws.avg_completion_percent) as avg_completion
                    FROM watch_stats ws
                    JOIN library_content lc ON ws.plex_rating_key = lc.plex_rating_key
                    WHERE lc.genres IS NOT NULL
                    GROUP BY ws.user_id, unnest(lc.genres)
                ),
                user_totals AS (
                    SELECT user_id, SUM(total_time) as user_total_time
                    FROM genre_stats
                    GROUP BY user_id
                )
                INSERT INTO user_genre_preferences (
                    user_id, genre, watch_count, total_watch_time_seconds,
                    avg_completion_percent, affinity_score
                )
                SELECT
                    gs.user_id,
                    gs.genre,
                    gs.watch_count,
                    gs.total_time,
                    gs.avg_completion,
                    CASE
                        WHEN ut.user_total_time > 0
                        THEN gs.total_time::decimal / ut.user_total_time
                        ELSE 0
                    END as affinity_score
                FROM genre_stats gs
                JOIN user_totals ut ON gs.user_id = ut.user_id
                ON CONFLICT (user_id, genre) DO UPDATE SET
                    watch_count = EXCLUDED.watch_count,
                    total_watch_time_seconds = EXCLUDED.total_watch_time_seconds,
                    avg_completion_percent = EXCLUDED.avg_completion_percent,
                    affinity_score = EXCLUDED.affinity_score,
                    updated_at = NOW()
                """
            )

    def close(self):
        """Clean up resources."""
        self.client.close()
