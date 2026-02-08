"""FastAPI application for Plex Recommender."""

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from plex_recommender.db import ensure_db_initialized
from plex_recommender.labels import PlexLabelService
from plex_recommender.logging import get_logger, setup_logging
from plex_recommender.recommend.service import RecommendationService
from plex_recommender.scheduler import JobScheduler
from plex_recommender.sync.plex import PlexSyncService
from plex_recommender.sync.tautulli import TautulliSyncService

logger = get_logger(__name__)
scheduler: JobScheduler | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global scheduler
    setup_logging()
    logger.info("starting_application")

    # Initialize database (auto-init if empty, run pending migrations)
    try:
        ensure_db_initialized()
    except Exception as e:
        logger.error("database_init_failed", error=str(e))

    # Start scheduler
    scheduler = JobScheduler()
    scheduler.start()

    yield

    # Shutdown
    if scheduler:
        scheduler.stop()
    logger.info("application_stopped")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Plex AI Recommender",
        description="AI-powered recommendation engine for Plex",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Health check
    @app.get("/health")
    async def health_check() -> dict[str, str]:
        return {"status": "healthy"}

    # Sync endpoints
    @app.post("/api/sync/tautulli")
    async def sync_tautulli(full: bool = False) -> dict[str, Any]:
        """Trigger Tautulli sync."""
        service = TautulliSyncService()
        try:
            count = service.sync_history(full_sync=full)
            return {"status": "success", "records_synced": count}
        except Exception as e:
            logger.error("sync_tautulli_failed", error=str(e))
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            service.close()

    @app.post("/api/sync/plex")
    async def sync_plex(library: str | None = None) -> dict[str, Any]:
        """Trigger Plex library sync."""
        service = PlexSyncService()
        try:
            count = service.sync_library(library_name=library)
            return {"status": "success", "items_synced": count}
        except Exception as e:
            logger.error("sync_plex_failed", error=str(e))
            raise HTTPException(status_code=500, detail=str(e))

    # Recommendation endpoints
    class GenerateRequest(BaseModel):
        user_id: int | None = None
        content_type: str = "movie"

    @app.post("/api/recommendations/generate")
    async def generate_recommendations(request: GenerateRequest) -> dict[str, Any]:
        """Generate recommendations."""
        service = RecommendationService()
        try:
            if request.user_id:
                count = service.generate_for_user(request.user_id, request.content_type)
                return {"status": "success", "user_id": request.user_id, "count": count}
            else:
                results = service.generate_for_all_users(request.content_type)
                return {"status": "success", "results": results}
        except Exception as e:
            logger.error("generate_recommendations_failed", error=str(e))
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            service.close()

    @app.get("/api/recommendations/{user_id}")
    async def get_recommendations(user_id: int) -> dict[str, Any]:
        """Get recommendations for a user."""
        service = RecommendationService()
        try:
            recs = service.get_active_recommendations(user_id)
            return {
                "user_id": user_id,
                "count": len(recs),
                "recommendations": recs,
            }
        finally:
            service.close()

    # Label endpoints
    @app.post("/api/labels/apply")
    async def apply_labels(user_id: int | None = None) -> dict[str, Any]:
        """Apply recommendation labels to Plex."""
        service = PlexLabelService()
        if user_id:
            results = service.apply_recommendation_labels(user_id)
            return {"status": "success", "user_id": user_id, "results": results}
        else:
            results = service.apply_for_all_users()
            return {"status": "success", "results": results}

    @app.post("/api/labels/cleanup")
    async def cleanup_labels(remove_all: bool = False) -> dict[str, Any]:
        """Clean up stale labels."""
        service = PlexLabelService()
        if remove_all:
            count = service.cleanup_all_ai_labels()
        else:
            count = service.remove_stale_labels()
        return {"status": "success", "labels_removed": count}

    @app.get("/api/labels/stats")
    async def label_stats() -> dict[str, Any]:
        """Get label statistics."""
        service = PlexLabelService()
        return service.get_label_stats()

    # Scheduler endpoints
    @app.get("/api/scheduler/jobs")
    async def get_jobs() -> dict[str, Any]:
        """Get scheduled jobs."""
        if scheduler:
            return {"jobs": scheduler.get_jobs()}
        return {"jobs": []}

    @app.post("/api/scheduler/run/{job_type}")
    async def run_job(job_type: str) -> dict[str, str]:
        """Manually trigger a job."""
        if not scheduler:
            raise HTTPException(status_code=503, detail="Scheduler not running")

        if job_type == "sync":
            scheduler.run_sync_now()
        elif job_type == "recommendations":
            scheduler.run_recommendations_now()
        else:
            raise HTTPException(status_code=400, detail=f"Unknown job type: {job_type}")

        return {"status": "triggered", "job": job_type}

    # Users endpoint
    @app.get("/api/users")
    async def get_users() -> dict[str, Any]:
        """Get all users."""
        from plex_recommender.db import get_db_cursor

        with get_db_cursor(commit=False) as cursor:
            cursor.execute(
                "SELECT id, plex_user_id, username, is_active FROM users ORDER BY username"
            )
            users = [dict(row) for row in cursor.fetchall()]
        return {"users": users}

    return app


# For direct uvicorn usage
app = create_app()
