"""Background job scheduler for sync and recommendation tasks."""

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from plex_recommender.config import get_settings
from plex_recommender.labels import PlexLabelService
from plex_recommender.logging import get_logger
from plex_recommender.recommend.service import RecommendationService
from plex_recommender.sync.plex import PlexSyncService
from plex_recommender.sync.tautulli import TautulliSyncService

logger = get_logger(__name__)


class JobScheduler:
    """Manages scheduled background jobs."""

    def __init__(self):
        self.scheduler = BackgroundScheduler()
        self.settings = get_settings()

    def _run_sync_job(self) -> None:
        """Run the sync job for Tautulli and Plex."""
        logger.info("scheduled_sync_started")
        try:
            # Sync Tautulli watch history
            tautulli_service = TautulliSyncService()
            try:
                tautulli_service.sync_history()
            finally:
                tautulli_service.close()

            # Sync Plex library (less frequently, but included for completeness)
            plex_service = PlexSyncService()
            plex_service.sync_library()

            logger.info("scheduled_sync_completed")
        except Exception as e:
            logger.error("scheduled_sync_failed", error=str(e))

    def _run_recommendation_job(self) -> None:
        """Run the recommendation generation job."""
        logger.info("scheduled_recommendations_started")
        try:
            # Clean up stale labels first
            label_service = PlexLabelService()
            label_service.remove_stale_labels()

            # Generate new recommendations
            rec_service = RecommendationService()
            try:
                # Generate for movies
                rec_service.generate_for_all_users("movie")
                # Generate for shows
                rec_service.generate_for_all_users("show")
            finally:
                rec_service.close()

            # Apply labels
            label_service.apply_for_all_users()

            # Collect feedback on previous recommendations
            self._run_feedback_job()

            logger.info("scheduled_recommendations_completed")
        except Exception as e:
            logger.error("scheduled_recommendations_failed", error=str(e))

    def _run_feedback_job(self) -> None:
        """Run the feedback collection job."""
        logger.info("scheduled_feedback_started")
        try:
            from plex_recommender.recommend.feedback import FeedbackService

            service = FeedbackService()
            results = service.collect_feedback_all_users()
            total = sum(sum(r.values()) for r in results.values())
            logger.info(
                "scheduled_feedback_completed",
                users=len(results),
                recommendations_updated=total,
            )
        except Exception as e:
            logger.error("scheduled_feedback_failed", error=str(e))

    def start(self) -> None:
        """Start the scheduler with configured jobs."""
        logger.info("starting_scheduler")

        # Sync job - runs every N minutes
        self.scheduler.add_job(
            self._run_sync_job,
            trigger=IntervalTrigger(minutes=self.settings.sync_interval_minutes),
            id="sync_job",
            name="Sync Tautulli and Plex data",
            replace_existing=True,
        )

        # Recommendation job - runs weekly
        day_map = {
            "monday": "mon",
            "tuesday": "tue",
            "wednesday": "wed",
            "thursday": "thu",
            "friday": "fri",
            "saturday": "sat",
            "sunday": "sun",
        }
        day = day_map.get(self.settings.recommendation_day.lower(), "sun")

        self.scheduler.add_job(
            self._run_recommendation_job,
            trigger=CronTrigger(
                day_of_week=day,
                hour=self.settings.recommendation_hour,
            ),
            id="recommendation_job",
            name="Generate weekly recommendations",
            replace_existing=True,
        )

        self.scheduler.start()
        logger.info(
            "scheduler_started",
            sync_interval=self.settings.sync_interval_minutes,
            rec_day=day,
            rec_hour=self.settings.recommendation_hour,
        )

    def stop(self) -> None:
        """Stop the scheduler."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("scheduler_stopped")

    def run_sync_now(self) -> None:
        """Trigger sync job immediately."""
        self._run_sync_job()

    def run_recommendations_now(self) -> None:
        """Trigger recommendation job immediately."""
        self._run_recommendation_job()

    def get_jobs(self) -> list[dict]:
        """Get list of scheduled jobs."""
        jobs = []
        for job in self.scheduler.get_jobs():
            jobs.append(
                {
                    "id": job.id,
                    "name": job.name,
                    "next_run": str(job.next_run_time),
                }
            )
        return jobs
