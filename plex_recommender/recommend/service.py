"""Recommendation service orchestrating data gathering and LLM calls."""

from datetime import UTC, datetime, timedelta

from psycopg2.extras import Json

from plex_recommender.config import get_settings
from plex_recommender.db import get_db_cursor
from plex_recommender.logging import get_logger
from plex_recommender.recommend.engine import RecommendationEngine, RecommendationItem
from plex_recommender.recommend.feedback import get_feedback_for_prompt

logger = get_logger(__name__)


class RecommendationService:
    """Service for orchestrating recommendation generation and storage."""

    def __init__(self):
        self.engine = RecommendationEngine()
        self.settings = get_settings()

    def get_user_preferences(self, user_id: int) -> dict:
        """Gather user preferences from the database."""
        preferences = {
            "genres": {},
            "preferred_content_rating": None,
            "avg_completion": 0,
            "peak_viewing_time": None,
        }

        with get_db_cursor(commit=False) as cursor:
            # Get genre preferences
            cursor.execute(
                """
                SELECT genre, affinity_score
                FROM user_genre_preferences
                WHERE user_id = %s
                ORDER BY affinity_score DESC
                LIMIT 20
                """,
                (user_id,),
            )
            rows = cursor.fetchall()
            for row in rows:
                if row is not None:
                    preferences["genres"][row["genre"]] = float(row["affinity_score"])

            # Get average completion rate
            cursor.execute(
                """
                SELECT AVG(avg_completion_percent) as avg_completion
                FROM watch_stats
                WHERE user_id = %s
                """,
                (user_id,),
            )
            row = cursor.fetchone()
            if row is not None and row.get("avg_completion") is not None:
                preferences["avg_completion"] = float(row["avg_completion"])

            # Get most common content rating
            cursor.execute(
                """
                SELECT lc.content_rating, COUNT(*) as count
                FROM watch_stats ws
                JOIN library_content lc ON ws.plex_rating_key = lc.plex_rating_key
                WHERE ws.user_id = %s AND lc.content_rating IS NOT NULL
                GROUP BY lc.content_rating
                ORDER BY count DESC
                LIMIT 1
                """,
                (user_id,),
            )
            row = cursor.fetchone()
            if row is not None:
                preferences["preferred_content_rating"] = row.get("content_rating")

        return preferences

    def get_watched_content(self, user_id: int, limit: int | None = None) -> list[dict]:
        """Get user's watched content with metadata.

        Args:
            user_id: User ID to get watch history for
            limit: Max items to return. None or 0 means no limit.
        """
        with get_db_cursor(commit=False) as cursor:
            query = """
                SELECT
                    lc.plex_rating_key,
                    lc.title,
                    lc.year,
                    lc.genres,
                    lc.content_type,
                    lc.rating,
                    lc.tmdb_rating,
                    ws.total_play_count as play_count,
                    ws.avg_completion_percent as avg_completion,
                    ws.last_watched_at
                FROM watch_stats ws
                JOIN library_content lc ON ws.plex_rating_key = lc.plex_rating_key
                WHERE ws.user_id = %s
                ORDER BY ws.last_watched_at DESC
            """
            params = [user_id]

            if limit and limit > 0:
                query += " LIMIT %s"
                params.append(limit)

            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    def get_unwatched_content(
        self,
        user_id: int,
        content_type: str | None = "movie",
        library_id: int | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Get unwatched content, optionally filtered by library.

        Args:
            user_id: User ID to check watch status against
            content_type: Filter by content type ('movie', 'show', or None for all)
            library_id: Filter by library section ID
            limit: Max items to return. None or 0 means no limit.
        """
        with get_db_cursor(commit=False) as cursor:
            # Build dynamic WHERE clause
            conditions = ["ws.id IS NULL"]
            params: list = []

            if content_type:
                conditions.append("lc.content_type = %s")
                params.append(content_type)

            if library_id:
                conditions.append("lc.library_section_id = %s")
                params.append(library_id)

            query = f"""
                SELECT
                    lc.plex_rating_key,
                    lc.title,
                    lc.year,
                    lc.genres,
                    lc.actors,
                    lc.keywords,
                    lc.languages,
                    lc.studio,
                    lc.summary,
                    lc.rating,
                    lc.tmdb_rating,
                    lc.content_rating,
                    lc.content_type,
                    lc.library_section_id
                FROM library_content lc
                LEFT JOIN watch_stats ws ON
                    lc.plex_rating_key = ws.plex_rating_key
                    AND ws.user_id = %s
                WHERE {" AND ".join(conditions)}
                ORDER BY COALESCE(lc.tmdb_rating, lc.rating) DESC NULLS LAST, lc.added_at DESC
            """
            query_params = [user_id] + params

            if limit and limit > 0:
                query += " LIMIT %s"
                query_params.append(limit)

            cursor.execute(query, query_params)
            return [dict(row) for row in cursor.fetchall()]

    def get_libraries(self) -> list[dict]:
        """Get list of available libraries from synced content."""
        with get_db_cursor(commit=False) as cursor:
            cursor.execute(
                """
                SELECT
                    lc.library_section_id as id,
                    COALESCE(ls.name, 'Library ' || lc.library_section_id::text) as name,
                    lc.content_type,
                    COUNT(*) as item_count
                FROM library_content lc
                LEFT JOIN library_sections ls ON lc.library_section_id = ls.section_id
                GROUP BY lc.library_section_id, ls.name, lc.content_type
                ORDER BY lc.library_section_id
                """
            )
            return [dict(row) for row in cursor.fetchall()]

    def save_recommendations(
        self,
        user_id: int,
        recommendations: list[RecommendationItem],
        prompt_hash: str,
        content_type: str = "movie",
        library_id: int | None = None,
    ) -> int:
        """Save recommendations to the database."""
        if not recommendations:
            return 0

        # Fetch watched keys once to avoid inserting already-watched items
        try:
            with get_db_cursor(commit=False) as cursor:
                cursor.execute(
                    "SELECT plex_rating_key FROM watch_stats WHERE user_id = %s",
                    (user_id,),
                )
                existing_watched = {row["plex_rating_key"] for row in cursor.fetchall()}
        except Exception:
            existing_watched = set()

        saved = 0
        expires_at = datetime.now(UTC) + timedelta(days=7)

        # Determine which model was actually used by the engine/client
        used_model = getattr(self.engine.client, "model", self.settings.ollama_model)

        with get_db_cursor() as cursor:
            for rec in recommendations:
                # Skip low confidence recommendations
                if rec.confidence < self.settings.min_confidence_score:
                    continue

                # Skip if the user has already watched this item
                if rec.rating_key in existing_watched:
                    logger.info(
                        "skipping_saving_watched_recommendation",
                        user_id=user_id,
                        rating_key=rec.rating_key,
                    )
                    continue

                cursor.execute(
                    """
                    INSERT INTO recommendations (
                        user_id, plex_rating_key, content_type, title,
                        confidence_score, reasoning, recommendation_factors,
                        model_used, prompt_hash, expires_at, library_section_id
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (user_id, plex_rating_key, generated_at) DO UPDATE SET
                        confidence_score = EXCLUDED.confidence_score,
                        reasoning = EXCLUDED.reasoning,
                        recommendation_factors = EXCLUDED.recommendation_factors,
                        library_section_id = EXCLUDED.library_section_id,
                        is_active = true
                    """,
                    (
                        user_id,
                        rec.rating_key,
                        content_type,
                        rec.title,
                        rec.confidence,
                        rec.reasoning,
                        Json({"factors": rec.matching_factors}),
                        used_model,
                        prompt_hash,
                        expires_at,
                        library_id,
                    ),
                )
                saved += 1

        logger.info("recommendations_saved", user_id=user_id, count=saved)
        return saved

    def deactivate_old_recommendations(
        self, user_id: int | None = None, library_id: int | None = None
    ) -> int:
        """Deactivate expired or old recommendations.

        Args:
            user_id: If provided, only deactivate for this user.
            library_id: If provided, only deactivate for this library section.
        """
        with get_db_cursor() as cursor:
            if user_id and library_id:
                # Deactivate only for specific user AND library
                cursor.execute(
                    """
                    UPDATE recommendations
                    SET is_active = false
                    WHERE user_id = %s
                        AND library_section_id = %s
                        AND is_active = true
                    RETURNING id
                    """,
                    (user_id, library_id),
                )
            elif user_id:
                # Deactivate all expired for user (not all active!)
                cursor.execute(
                    """
                    UPDATE recommendations
                    SET is_active = false
                    WHERE user_id = %s
                        AND expires_at < NOW()
                        AND is_active = true
                    RETURNING id
                    """,
                    (user_id,),
                )
            else:
                # Deactivate all expired recommendations
                cursor.execute(
                    """
                    UPDATE recommendations
                    SET is_active = false
                    WHERE expires_at < NOW() AND is_active = true
                    RETURNING id
                    """
                )
            deactivated = len(cursor.fetchall())

        if deactivated:
            logger.info(
                "recommendations_deactivated",
                count=deactivated,
                user_id=user_id,
                library_id=library_id,
            )
        return deactivated

    def generate_for_user(
        self,
        user_id: int,
        content_type: str | None = "movie",
        library_id: int | None = None,
    ) -> int:
        """Generate and save recommendations for a single user.

        Uses RAG (Retrieval-Augmented Generation) when enabled to pre-filter
        the most relevant content before sending to the LLM. Falls back to
        batch processing for large libraries when RAG is disabled.
        """
        logger.info(
            "generating_for_user", user_id=user_id, content_type=content_type, library_id=library_id
        )

        # Deactivate old recommendations for this specific library only
        self.deactivate_old_recommendations(user_id, library_id)

        # Get limits from config (0 = unlimited)
        watch_limit = (
            self.settings.max_watch_history_items
            if self.settings.max_watch_history_items > 0
            else None
        )

        # Gather user data
        preferences = self.get_user_preferences(user_id)
        watched = self.get_watched_content(user_id, limit=watch_limit)

        # Deactivate any existing active recommendations for items the user
        # has already watched to avoid showing stale suggestions.
        try:
            watched_keys = {w.get("plex_rating_key") for w in watched}
            if watched_keys:
                with get_db_cursor() as cursor:
                    if library_id is not None:
                        cursor.execute(
                            """
                            UPDATE recommendations
                            SET is_active = false
                            WHERE user_id = %s
                              AND plex_rating_key = ANY(%s)
                              AND library_section_id = %s
                              AND is_active = true
                            """,
                            (user_id, list(watched_keys), library_id),
                        )
                    else:
                        cursor.execute(
                            """
                            UPDATE recommendations
                            SET is_active = false
                            WHERE user_id = %s
                              AND plex_rating_key = ANY(%s)
                              AND is_active = true
                            """,
                            (user_id, list(watched_keys)),
                        )
        except Exception:
            # Non-fatal; continue generation even if cleanup fails
            logger.debug("failed_to_deactivate_watched_recommendations", user_id=user_id)

        # Try RAG-based retrieval first
        if self.settings.use_rag:
            # Determine an adaptive limit for RAG results so we don't always
            # return the static `rag_top_k` (which defaults to 200). Prefer a
            # batch-size sized candidate set, but respect any explicit
            # `max_library_items` limit.
            desired_limit = self.settings.rag_top_k
            try:
                batch_based = self.settings.get_effective_batch_size()
                desired_limit = min(desired_limit, batch_based)
            except Exception:
                # Fall back to rag_top_k if batch calc fails for any reason
                desired_limit = self.settings.rag_top_k

            if self.settings.max_library_items > 0:
                desired_limit = min(desired_limit, self.settings.max_library_items)

            available = self._get_content_via_rag(user_id, library_id, limit=desired_limit)
            if available:
                logger.info(
                    "using_rag_retrieval",
                    user_id=user_id,
                    retrieved_count=len(available),
                )
                return self._generate_single(
                    user_id=user_id,
                    preferences=preferences,
                    watched=watched,
                    available=available,
                    content_type=content_type,
                    library_id=library_id,
                )
            else:
                logger.warning(
                    "rag_retrieval_empty_fallback",
                    user_id=user_id,
                    message="RAG returned no results, falling back to batch processing",
                )

        # Fallback: Get ALL unwatched content and batch process
        all_available = self.get_unwatched_content(
            user_id,
            content_type,
            library_id=library_id,
            limit=None,  # Get everything
        )

        if not all_available:
            logger.warning("no_unwatched_content", user_id=user_id)
            return 0

        total_items = len(all_available)
        batch_size = self.settings.get_effective_batch_size()
        use_batching = (
            self.settings.batch_processing
            and self.settings.max_library_items == 0  # Only batch if no hard limit set
            and total_items > batch_size
        )

        if use_batching:
            logger.info(
                "batch_size_calculated",
                batch_size=batch_size,
                context_window=self.settings.ollama_num_ctx,
                compact=self.settings.compact_prompt,
            )
            # Process in batches
            return self._generate_batched(
                user_id=user_id,
                preferences=preferences,
                watched=watched,
                all_available=all_available,
                content_type=content_type,
                batch_size=batch_size,
                library_id=library_id,
            )
        else:
            # Single call (apply limit if set)
            if self.settings.max_library_items > 0:
                all_available = all_available[: self.settings.max_library_items]

            return self._generate_single(
                user_id=user_id,
                preferences=preferences,
                watched=watched,
                available=all_available,
                content_type=content_type,
                library_id=library_id,
            )

    def _get_content_via_rag(
        self,
        user_id: int,
        library_id: int | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Retrieve relevant content using RAG vector search.

        Args:
            user_id: The user's database ID.
            library_id: Optional filter by library section.

        Returns:
            List of relevant unwatched content dicts, or empty list if RAG unavailable.
        """
        try:
            from plex_recommender.embeddings import EmbeddingsService

            service = EmbeddingsService()
            try:
                # Get relevant content via vector similarity search
                # Compute effective limit: prefer provided `limit`, then cap by
                # configured `rag_top_k`. This allows RAG to adapt to the
                # effective batch size / max_library_items and not be hard-locked
                # to the static `rag_top_k` value.
                effective_limit = limit or self.settings.rag_top_k
                import time

                start = time.perf_counter()
                relevant = service.search_for_user(
                    user_id=user_id,
                    library_section_id=library_id,
                    limit=effective_limit,
                )
                elapsed = time.perf_counter() - start
                logger.info(
                    "rag_search_timing",
                    user_id=user_id,
                    library_id=library_id,
                    limit=effective_limit,
                    result_count=len(relevant) if relevant else 0,
                    elapsed_seconds=f"{elapsed:.2f}",
                )

                if not relevant:
                    return []

                # Optionally filter out any already-watched content (double-check)
                if self.settings.exclude_watched_from_llm:
                    with get_db_cursor(commit=False) as cursor:
                        cursor.execute(
                            "SELECT plex_rating_key FROM watch_stats WHERE user_id = %s",
                            (user_id,),
                        )
                        watched_keys = {row["plex_rating_key"] for row in cursor.fetchall()}

                    return [r for r in relevant if r.get("plex_rating_key") not in watched_keys]
                else:
                    return relevant

            finally:
                service.close()

        except ImportError:
            logger.warning("embeddings_module_not_available")
            return []
        except Exception as e:
            logger.warning("rag_retrieval_failed", error=str(e))
            return []

    def _generate_single(
        self,
        user_id: int,
        preferences: dict,
        watched: list[dict],
        available: list[dict],
        content_type: str | None,
        library_id: int | None = None,
    ) -> int:
        """Generate recommendations in a single LLM call."""
        from plex_recommender.recommend.engine import OllamaError, OpenRouterError

        # Fetch feedback history to improve recommendations
        feedback_history = get_feedback_for_prompt(user_id)
        feedback_count = sum(len(v) for v in feedback_history.values())
        if feedback_count > 0:
            logger.info(
                "using_feedback_history",
                user_id=user_id,
                loved=len(feedback_history.get("loved", [])),
                liked=len(feedback_history.get("liked", [])),
                disliked=len(feedback_history.get("disliked", [])),
                skipped=len(feedback_history.get("skipped", [])),
            )

        # Optionally remove any available items that are already watched
        if self.settings.exclude_watched_from_llm and watched:
            watched_keys = {w.get("plex_rating_key") for w in watched}
            original_count = len(available)
            available = [a for a in available if a.get("plex_rating_key") not in watched_keys]
            removed = original_count - len(available)
            if removed:
                logger.info(
                    "filtered_available_removed_watched",
                    user_id=user_id,
                    removed=removed,
                )

        if not available:
            logger.info("no_available_after_filtering", user_id=user_id)
            return 0

        try:
            recommendations, prompt_hash = self.engine.generate_recommendations(
                user_id=user_id,
                user_preferences=preferences,
                watched_content=watched,
                available_content=available,
                feedback_history=feedback_history,
            )
        except (OllamaError, OpenRouterError) as e:
            logger.error("llm_generation_failed", user_id=user_id, error=str(e))
            return 0
        except Exception as e:
            logger.error("unexpected_generation_error", user_id=user_id, error=str(e))
            return 0

        save_content_type = content_type or (
            available[0].get("content_type", "movie") if available else "movie"
        )

        return self.save_recommendations(
            user_id, recommendations, prompt_hash, save_content_type, library_id
        )

    def _generate_batched(
        self,
        user_id: int,
        preferences: dict,
        watched: list[dict],
        all_available: list[dict],
        content_type: str | None,
        batch_size: int | None = None,
        library_id: int | None = None,
    ) -> int:
        """Generate recommendations in batches, aggregate results."""
        # Optionally filter out watched items from the available pool before batching
        if self.settings.exclude_watched_from_llm and watched:
            watched_keys = {w.get("plex_rating_key") for w in watched}
            original_total = len(all_available)
            all_available = [
                a for a in all_available if a.get("plex_rating_key") not in watched_keys
            ]
            removed = original_total - len(all_available)
            if removed:
                logger.info(
                    "filtered_watched_from_all_available",
                    user_id=user_id,
                    removed=removed,
                )

        if batch_size is None:
            batch_size = self.settings.get_effective_batch_size()
        total_items = len(all_available)
        num_batches = (total_items + batch_size - 1) // batch_size  # Ceiling division

        logger.info(
            "batched_generation_started",
            user_id=user_id,
            total_items=total_items,
            batch_size=batch_size,
            num_batches=num_batches,
        )

        # Fetch feedback history once for all batches
        feedback_history = get_feedback_for_prompt(user_id)
        feedback_count = sum(len(v) for v in feedback_history.values())
        if feedback_count > 0:
            logger.info(
                "using_feedback_history",
                user_id=user_id,
                loved=len(feedback_history.get("loved", [])),
                liked=len(feedback_history.get("liked", [])),
                disliked=len(feedback_history.get("disliked", [])),
                skipped=len(feedback_history.get("skipped", [])),
            )

        all_recommendations: list[RecommendationItem] = []
        seen_keys: set[str] = set()

        for batch_num in range(num_batches):
            start_idx = batch_num * batch_size
            end_idx = min(start_idx + batch_size, total_items)
            batch = all_available[start_idx:end_idx]

            logger.info(
                "processing_batch",
                batch=batch_num + 1,
                of=num_batches,
                items=len(batch),
            )

            try:
                recommendations, _ = self.engine.generate_recommendations(
                    user_id=user_id,
                    user_preferences=preferences,
                    watched_content=watched,
                    available_content=batch,
                    feedback_history=feedback_history,
                )

                # Deduplicate (same item might be recommended in multiple batches)
                for rec in recommendations:
                    if rec.rating_key not in seen_keys:
                        all_recommendations.append(rec)
                        seen_keys.add(rec.rating_key)

                logger.info(
                    "batch_complete",
                    batch=batch_num + 1,
                    found=len(recommendations),
                    total_so_far=len(all_recommendations),
                )

            except Exception as e:
                logger.error(
                    "batch_failed",
                    batch=batch_num + 1,
                    of=num_batches,
                    error=str(e),
                )
                # Continue with other batches instead of stopping

        # Sort all recommendations by confidence and take top N
        all_recommendations.sort(key=lambda r: r.confidence, reverse=True)
        max_recs = self.settings.max_recommendations_per_user
        top_recommendations = all_recommendations[:max_recs]

        logger.info(
            "batched_generation_complete",
            total_found=len(all_recommendations),
            keeping=len(top_recommendations),
        )

        # Save the aggregated top recommendations
        save_content_type = content_type or (
            all_available[0].get("content_type", "movie") if all_available else "movie"
        )

        # Use a combined prompt hash for batched results
        import hashlib

        prompt_hash = hashlib.md5(f"batched_{user_id}_{total_items}".encode()).hexdigest()[:16]

        return self.save_recommendations(
            user_id, top_recommendations, prompt_hash, save_content_type, library_id
        )

    def generate_for_all_users(
        self, content_type: str | None = None, library_id: int | None = None
    ) -> dict[int, int]:
        """Generate recommendations for all active users.

        If no library_id is specified, generates per-library for each user.
        """
        logger.info("generating_for_all_users", content_type=content_type, library_id=library_id)
        results = {}

        with get_db_cursor(commit=False) as cursor:
            cursor.execute("SELECT id FROM users WHERE is_active = true")
            user_ids = [row["id"] for row in cursor.fetchall()]

        # Get libraries if not specified
        libraries = None
        if library_id is None:
            libraries = self.get_libraries()

        for user_id in user_ids:
            user_count = 0
            try:
                if libraries:
                    # Run per library
                    for lib in libraries:
                        lib_id = lib["id"]
                        lib_type = lib["content_type"]
                        count = self.generate_for_user(user_id, lib_type, lib_id)
                        user_count += count
                else:
                    # Single library specified
                    user_count = self.generate_for_user(user_id, content_type, library_id)

                results[user_id] = user_count
            except Exception as e:
                logger.error(
                    "user_recommendation_failed",
                    user_id=user_id,
                    error=str(e),
                )
                results[user_id] = -1

        logger.info("all_users_complete", results=results)
        return results

    def get_active_recommendations(self, user_id: int) -> list[dict]:
        """Get active recommendations for a user."""
        with get_db_cursor(commit=False) as cursor:
            cursor.execute(
                """
                SELECT
                    r.*,
                    lc.summary,
                    lc.genres,
                    lc.thumb_url
                FROM recommendations r
                JOIN library_content lc ON r.plex_rating_key = lc.plex_rating_key
                WHERE r.user_id = %s
                    AND r.is_active = true
                    AND (r.expires_at IS NULL OR r.expires_at > NOW())
                ORDER BY r.confidence_score DESC
                """,
                (user_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def close(self):
        """Clean up resources."""
        self.engine.close()
