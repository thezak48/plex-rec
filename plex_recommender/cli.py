"""Command-line interface for Plex Recommender."""

import typer
from rich.console import Console
from rich.table import Table

from plex_recommender.config import get_settings
from plex_recommender.db import ensure_db_initialized, init_db
from plex_recommender.labels import PlexLabelService
from plex_recommender.logging import get_logger, setup_logging
from plex_recommender.recommend.service import RecommendationService
from plex_recommender.scheduler import JobScheduler
from plex_recommender.sync.plex import PlexSyncService
from plex_recommender.sync.tautulli import TautulliSyncService

app = typer.Typer(
    name="plex-rec",
    help="AI-powered recommendation engine for Plex",
    add_completion=False,
)
console = Console()
logger = get_logger(__name__)


@app.callback()
def main(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose output"),
):
    """Plex AI Recommendation Engine CLI."""
    log_level = "DEBUG" if verbose else "INFO"
    setup_logging(log_level)

    # Auto-initialize database if needed (skip for db commands which handle this themselves)
    import sys

    if len(sys.argv) > 1 and sys.argv[1] != "db":
        try:
            ensure_db_initialized()
        except Exception as e:
            console.print(f"[red]✗ Database initialization failed: {e}[/red]")
            raise typer.Exit(1)


# Database commands
db_app = typer.Typer(help="Database management commands")
app.add_typer(db_app, name="db")


@db_app.command("init")
def db_init():
    """Initialize the database schema and run migrations."""
    console.print("[cyan]Initializing database...[/cyan]")
    try:
        init_db()
        console.print("[green]✓ Database initialized successfully[/green]")
        console.print("[dim]Schema created and all migrations applied[/dim]")
    except Exception as e:
        console.print(f"[red]✗ Database initialization failed: {e}[/red]")
        raise typer.Exit(1)


@db_app.command("reset")
def db_reset(
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation"),
):
    """Reset the database (drops all tables)."""
    if not force:
        confirm = typer.confirm("This will delete all data. Are you sure?")
        if not confirm:
            raise typer.Abort()

    from plex_recommender.db.schema import create_tables, drop_tables

    console.print("[yellow]Dropping tables...[/yellow]")
    drop_tables()
    console.print("[cyan]Recreating tables...[/cyan]")
    create_tables()

    # Also clear embeddings since they reference deleted content
    try:
        from plex_recommender.embeddings import VectorStore

        vs = VectorStore()
        vs.clear()
        console.print("[cyan]Cleared embeddings[/cyan]")
    except Exception:
        pass  # Embeddings may not exist yet

    console.print("[green]✓ Database reset complete[/green]")


@db_app.command("migrate")
def db_migrate():
    """Run all pending database migrations."""
    from plex_recommender.db.migrations import run_migrations

    console.print("[cyan]Checking for pending migrations...[/cyan]")
    try:
        count = run_migrations()
        if count > 0:
            console.print(f"[green]✓ Applied {count} migration(s)[/green]")
            console.print("[dim]Run 'plex-rec sync plex' to populate new fields[/dim]")
        else:
            console.print("[green]✓ Database is up to date[/green]")
    except Exception as e:
        console.print(f"[red]✗ Migration failed: {e}[/red]")
        raise typer.Exit(1)


@db_app.command("migrate-status")
def db_migrate_status():
    """Show status of all database migrations."""
    from rich.table import Table

    from plex_recommender.db.migrations import get_migration_status

    status = get_migration_status()

    if not status:
        console.print("[dim]No migrations found[/dim]")
        return

    table = Table(title="Database Migrations")
    table.add_column("Version", style="cyan")
    table.add_column("Name", style="white")
    table.add_column("Status", style="green")
    table.add_column("Applied At", style="dim")

    for migration in status:
        status_style = "green" if migration["status"] == "applied" else "yellow"
        applied_at = str(migration["applied_at"])[:19] if migration["applied_at"] else "-"
        table.add_row(
            migration["version"],
            migration["name"],
            f"[{status_style}]{migration['status']}[/{status_style}]",
            applied_at,
        )

    console.print(table)


# Sync commands
sync_app = typer.Typer(help="Data synchronization commands")
app.add_typer(sync_app, name="sync")


@sync_app.command("tautulli")
def sync_tautulli(
    full: bool = typer.Option(False, "--full", help="Force full sync instead of incremental"),
):
    """Sync watch history from Tautulli."""
    console.print("[cyan]Syncing from Tautulli...[/cyan]")
    service = TautulliSyncService()
    try:
        count = service.sync_history(full_sync=full)
        console.print(f"[green]✓ Synced {count} history records[/green]")
    except Exception as e:
        console.print(f"[red]✗ Sync failed: {e}[/red]")
        raise typer.Exit(1)
    finally:
        service.close()


@sync_app.command("plex")
def sync_plex(
    library: str | None = typer.Option(None, "--library", "-l", help="Specific library to sync"),
):
    """Sync library content from Plex."""
    console.print("[cyan]Syncing from Plex...[/cyan]")
    service = PlexSyncService()
    try:
        count = service.sync_library(library_name=library)
        console.print(f"[green]✓ Synced {count} library items[/green]")
    except Exception as e:
        console.print(f"[red]✗ Sync failed: {e}[/red]")
        raise typer.Exit(1)


@sync_app.command("all")
def sync_all(
    full: bool = typer.Option(False, "--full", help="Force full sync"),
    skip_embeddings: bool = typer.Option(
        False, "--skip-embeddings", help="Skip embedding generation after sync"
    ),
):
    """Sync all data from Tautulli and Plex, then generate embeddings for RAG."""
    sync_tautulli(full=full)
    sync_plex(library=None)

    # Re-run genre preferences update now that library content is synced
    # (Genre preferences require library_content to be populated for genre data)
    console.print("[cyan]Updating genre preferences...[/cyan]")
    try:
        service = TautulliSyncService()
        service._update_genre_preferences()
        console.print("[green]✓ Genre preferences updated[/green]")
    except Exception as e:
        console.print(f"[yellow]⚠ Genre preferences update failed: {e}[/yellow]")

    # Enrich with TMDB data if configured
    settings = get_settings()
    if settings.tmdb_api_token:
        console.print("[cyan]Enriching with TMDB data...[/cyan]")
        try:
            from plex_recommender.sync.tmdb import TMDBEnrichmentService

            tmdb_service = TMDBEnrichmentService()
            count = tmdb_service.enrich_keywords(limit=None)
            console.print(f"[green]✓ Enriched {count} items with TMDB data[/green]")
        except Exception as e:
            console.print(f"[yellow]⚠ TMDB enrichment failed: {e}[/yellow]")
    else:
        console.print("[dim]Skipping TMDB enrichment (TMDB_API_TOKEN not configured)[/dim]")

    # Generate embeddings for RAG if enabled
    if settings.use_rag and not skip_embeddings:
        console.print("[cyan]Generating embeddings for RAG...[/cyan]")
        try:
            from plex_recommender.embeddings import EmbeddingsService
            from plex_recommender.recommend.engine import OllamaClient

            # Ensure embeddings model is available
            client = OllamaClient(model=settings.embeddings_model)
            if not client.is_model_available(settings.embeddings_model):
                console.print(
                    f"[yellow]Pulling embeddings model {settings.embeddings_model}...[/yellow]"
                )
                client.pull_model(settings.embeddings_model)
            client.close()

            service = EmbeddingsService()
            result = service.generate_embeddings()
            service.close()

            if result["status"] == "success":
                console.print(
                    f"[green]✓ Generated embeddings for {result['processed']} items[/green]"
                )
            else:
                console.print(
                    f"[yellow]⚠ Embedding generation incomplete: {result['status']}[/yellow]"
                )
        except Exception as e:
            console.print(f"[yellow]⚠ Embedding generation failed: {e}[/yellow]")
            console.print("[dim]Run 'plex-rec embeddings generate --ensure-model' manually[/dim]")
    elif not settings.use_rag:
        console.print("[dim]Skipping embedding generation (USE_RAG=false)[/dim]")
    else:
        console.print("[dim]Skipping embedding generation (--skip-embeddings)[/dim]")


@sync_app.command("tmdb")
def sync_tmdb(
    limit: int | None = typer.Option(None, "--limit", "-n", help="Max items to enrich"),
):
    """Enrich library content with TMDB keywords.

    Requires TMDB_API_TOKEN to be set in environment.
    """
    settings = get_settings()
    if not settings.tmdb_api_token:
        console.print("[red]✗ TMDB_API_TOKEN is not configured[/red]")
        console.print("[dim]Get your API token from https://www.themoviedb.org/settings/api[/dim]")
        raise typer.Exit(1)

    console.print("[cyan]Enriching library with TMDB keywords...[/cyan]")
    try:
        from plex_recommender.sync.tmdb import TMDBEnrichmentService

        service = TMDBEnrichmentService()
        count = service.enrich_keywords(limit=limit)
        console.print(f"[green]✓ Enriched {count} items with TMDB keywords[/green]")
    except Exception as e:
        console.print(f"[red]✗ TMDB enrichment failed: {e}[/red]")
        raise typer.Exit(1)


# Recommendation commands
rec_app = typer.Typer(help="Recommendation generation commands")
app.add_typer(rec_app, name="recommend")


@rec_app.command("generate")
def recommend_generate(
    user_id: int | None = typer.Option(None, "--user", "-u", help="Specific user ID"),
    content_type: str | None = typer.Option(
        None, "--type", "-t", help="Content type (movie/show) or omit to auto-detect from library"
    ),
    library_id: int | None = typer.Option(
        None, "--library", "-l", help="Specific library section ID (omit to run all libraries)"
    ),
    ensure_model: bool = typer.Option(
        False, "--ensure-model", "-e", help="Pull model if not available"
    ),
):
    """Generate recommendations.

    By default, processes each library separately for better results.
    Use --library to target a specific library.
    """
    from plex_recommender.recommend.engine import OllamaClient

    # Optionally ensure model is available
    if ensure_model:
        client = OllamaClient()
        try:
            if not client.check_health():
                console.print("[red]✗ Cannot connect to Ollama[/red]")
                raise typer.Exit(1)

            settings = get_settings()
            if not client.is_model_available():
                console.print(f"[yellow]Pulling model {settings.ollama_model}...[/yellow]")
                if not client.pull_model():
                    console.print("[red]✗ Failed to pull model[/red]")
                    raise typer.Exit(1)
                console.print("[green]✓ Model ready[/green]")
        finally:
            client.close()

    service = RecommendationService()
    try:
        if user_id:
            if library_id:
                # Single library specified
                console.print(
                    f"[cyan]Generating recommendations for user {user_id}, library {library_id}...[/cyan]"
                )
                count = service.generate_for_user(user_id, content_type, library_id)
                console.print(f"[green]✓ Generated {count} recommendations[/green]")
            else:
                # Run per library (default behavior)
                libraries = service.get_libraries()
                if not libraries:
                    console.print(
                        "[yellow]No libraries found. Run 'plex-rec sync plex' first.[/yellow]"
                    )
                    raise typer.Exit(1)

                total_count = 0
                console.print(
                    f"[cyan]Generating recommendations for user {user_id} across {len(libraries)} libraries...[/cyan]"
                )

                for lib in libraries:
                    lib_id = lib["id"]
                    lib_name = lib["name"]
                    lib_type = lib["content_type"]
                    item_count = lib["item_count"]

                    console.print(
                        f"  [dim]Processing {lib_name} ({item_count} {lib_type}s)...[/dim]"
                    )
                    try:
                        count = service.generate_for_user(user_id, lib_type, lib_id)
                        total_count += count
                        if count > 0:
                            console.print(f"    [green]✓ {count} recommendations[/green]")
                        else:
                            console.print("    [dim]No recommendations[/dim]")
                    except Exception as e:
                        console.print(f"    [red]✗ Failed: {e}[/red]")

                console.print(
                    f"[green]✓ Generated {total_count} total recommendations across all libraries[/green]"
                )
        else:
            # All users - also runs per library
            console.print("[cyan]Generating recommendations for all users...[/cyan]")
            results = service.generate_for_all_users(content_type, library_id)
            total = sum(v for v in results.values() if v > 0)
            console.print(f"[green]✓ Generated {total} total recommendations[/green]")
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]✗ Generation failed: {e}[/red]")
        raise typer.Exit(1)
    finally:
        service.close()


@rec_app.command("libraries")
def recommend_libraries():
    """List available libraries for recommendations."""
    service = RecommendationService()
    try:
        libraries = service.get_libraries()

        if not libraries:
            console.print("[yellow]No libraries found. Run 'plex-rec sync plex' first.[/yellow]")
            return

        table = Table(title="Available Libraries")
        table.add_column("ID", style="cyan", justify="right")
        table.add_column("Name", style="green")
        table.add_column("Type")
        table.add_column("Items", justify="right")

        for lib in libraries:
            table.add_row(
                str(lib["id"]),
                lib["name"],
                lib["content_type"],
                str(lib["item_count"]),
            )

        console.print(table)
        console.print("\n[dim]Use --library/-l with the Library ID to filter recommendations[/dim]")
    finally:
        service.close()


@rec_app.command("list")
def recommend_list(
    user_id: int = typer.Option(..., "--user", "-u", help="User ID to list recommendations for"),
):
    """List active recommendations for a user."""
    service = RecommendationService()
    try:
        recs = service.get_active_recommendations(user_id)

        if not recs:
            console.print("[yellow]No active recommendations found[/yellow]")
            return

        table = Table(title=f"Recommendations for User {user_id}")
        table.add_column("Title", style="cyan")
        table.add_column("Confidence", justify="right")
        table.add_column("Feedback", justify="center")
        table.add_column("Reasoning")

        for rec in recs:
            confidence = f"{rec['confidence_score']:.0%}"
            feedback = rec.get("user_feedback") or "-"
            table.add_row(
                rec["title"],
                confidence,
                feedback,
                rec["reasoning"][:50] + "..." if len(rec["reasoning"]) > 50 else rec["reasoning"],
            )

        console.print(table)
    finally:
        service.close()


@rec_app.command("feedback")
def recommend_feedback(
    user_id: int | None = typer.Option(None, "--user", "-u", help="Specific user ID"),
    stats_only: bool = typer.Option(False, "--stats", "-s", help="Show statistics only"),
):
    """Collect feedback on recommendations based on watch history and ratings."""
    from plex_recommender.recommend.feedback import FeedbackService

    service = FeedbackService()

    if stats_only:
        # Just show statistics
        stats = service.get_feedback_stats(user_id)

        table = Table(title="Recommendation Feedback Statistics")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", justify="right")

        # Totals
        table.add_row("Total Recommendations", str(stats["totals"]["total_recommendations"]))
        table.add_row("With Feedback", str(stats["totals"]["with_feedback"]))
        table.add_row("Active", str(stats["totals"]["active"]))
        table.add_row("", "")

        # Feedback breakdown
        table.add_row("[bold]Feedback Breakdown[/bold]", "")
        for feedback_type, count in stats["feedback_counts"].items():
            emoji = {
                "loved": "❤️",
                "liked": "👍",
                "completed": "✅",
                "watched": "👁️",
                "disliked": "👎",
                "skipped": "⏭️",
            }.get(feedback_type, "")
            table.add_row(f"  {emoji} {feedback_type}", str(count))

        table.add_row("", "")
        table.add_row("[bold]Quality Metrics[/bold]", "")
        table.add_row("  Positive (loved/liked/completed)", str(stats["quality"]["positive"]))
        table.add_row("  Neutral (watched)", str(stats["quality"]["neutral"]))
        table.add_row("  Negative (disliked/skipped)", str(stats["quality"]["negative"]))
        table.add_row("  [green]Hit Rate[/green]", f"{stats['quality']['hit_rate_percent']}%")

        console.print(table)
        return

    # Collect feedback
    if user_id:
        console.print(f"[cyan]Collecting feedback for user {user_id}...[/cyan]")
        results = service.collect_feedback_for_user(user_id)
        if results:
            for feedback_type, count in results.items():
                console.print(f"  {feedback_type}: {count}")
            console.print(f"[green]✓ Updated {sum(results.values())} recommendations[/green]")
        else:
            console.print("[yellow]No feedback to collect[/yellow]")
    else:
        console.print("[cyan]Collecting feedback for all users...[/cyan]")
        all_results = service.collect_feedback_all_users()
        total = sum(sum(r.values()) for r in all_results.values())
        console.print(
            f"[green]✓ Updated feedback for {len(all_results)} users ({total} recommendations)[/green]"
        )


@rec_app.command("feedback-stats")
def recommend_feedback_stats(
    user_id: int | None = typer.Option(None, "--user", "-u", help="Specific user ID"),
):
    """Show feedback statistics for recommendations."""
    # Delegate to feedback command with stats flag
    recommend_feedback(user_id=user_id, stats_only=True)


# Model commands
model_app = typer.Typer(help="Ollama model management commands")
app.add_typer(model_app, name="model")


@model_app.command("list")
def model_list():
    """List available Ollama models."""
    from plex_recommender.recommend.engine import OllamaClient

    client = OllamaClient()
    try:
        if not client.check_health():
            console.print("[red]✗ Cannot connect to Ollama[/red]")
            raise typer.Exit(1)

        models = client.list_models()
        settings = get_settings()

        if not models:
            console.print("[yellow]No models found in Ollama[/yellow]")
            return

        table = Table(title="Available Ollama Models")
        table.add_column("Model", style="cyan")
        table.add_column("Active", justify="center")

        for model in models:
            is_active = (
                "✓"
                if model == settings.ollama_model or model.startswith(f"{settings.ollama_model}:")
                else ""
            )
            table.add_row(model, is_active)

        console.print(table)
    finally:
        client.close()


@model_app.command("pull")
def model_pull(
    model: str | None = typer.Option(
        None, "--model", "-m", help="Model to pull (default: configured model)"
    ),
):
    """Pull/download a model from Ollama registry."""
    from plex_recommender.recommend.engine import OllamaClient

    client = OllamaClient()
    settings = get_settings()
    model_name = model or settings.ollama_model

    try:
        if not client.check_health():
            console.print("[red]✗ Cannot connect to Ollama[/red]")
            raise typer.Exit(1)

        console.print(f"[cyan]Pulling model {model_name}...[/cyan]")
        console.print("[dim]This may take a while for large models[/dim]")

        success = client.pull_model(model_name)

        if success:
            console.print(f"[green]✓ Model {model_name} pulled successfully[/green]")
        else:
            console.print(f"[red]✗ Failed to pull model {model_name}[/red]")
            raise typer.Exit(1)
    finally:
        client.close()


@model_app.command("ensure")
def model_ensure(
    model: str | None = typer.Option(
        None, "--model", "-m", help="Model to ensure (default: configured model)"
    ),
):
    """Ensure a model is available, pulling if necessary."""
    from plex_recommender.recommend.engine import OllamaClient

    client = OllamaClient()
    settings = get_settings()
    model_name = model or settings.ollama_model

    try:
        if not client.check_health():
            console.print("[red]✗ Cannot connect to Ollama[/red]")
            raise typer.Exit(1)

        console.print(f"[cyan]Checking model {model_name}...[/cyan]")

        if client.is_model_available(model_name):
            console.print(f"[green]✓ Model {model_name} is already available[/green]")
        else:
            console.print(f"[yellow]Model not found, pulling {model_name}...[/yellow]")
            success = client.pull_model(model_name)
            if success:
                console.print(f"[green]✓ Model {model_name} is now available[/green]")
            else:
                console.print(f"[red]✗ Failed to pull model {model_name}[/red]")
                raise typer.Exit(1)
    finally:
        client.close()


@model_app.command("check")
def model_check():
    """Check LLM connectivity and model status."""
    from plex_recommender.recommend.engine import OllamaClient, OpenRouterClient

    settings = get_settings()

    if settings.llm_provider == "openrouter":
        # OpenRouter mode
        table = Table(title="OpenRouter Status")
        table.add_column("Check", style="cyan")
        table.add_column("Status")
        table.add_column("Details")

        try:
            client = OpenRouterClient()
        except ValueError as e:
            table.add_row("API Key", "[red]✗[/red]", str(e))
            console.print(table)
            raise typer.Exit(1)

        healthy = client.check_health()
        table.add_row(
            "Connectivity",
            "[green]✓[/green]" if healthy else "[red]✗[/red]",
            settings.openrouter_base_url,
        )

        table.add_row(
            "Configured Model",
            "[green]✓[/green]",
            settings.openrouter_model,
        )

        table.add_row(
            "Context Window",
            "[green]✓[/green]",
            f"{settings.openrouter_context_window:,} tokens",
        )

        console.print(table)
        client.close()

        if not healthy:
            raise typer.Exit(1)
    else:
        # Ollama mode
        client = OllamaClient()

        table = Table(title="Ollama Status")
        table.add_column("Check", style="cyan")
        table.add_column("Status")
        table.add_column("Details")

        # Check connectivity
        healthy = client.check_health()
        table.add_row(
            "Connectivity",
            "[green]✓[/green]" if healthy else "[red]✗[/red]",
            settings.ollama_url,
        )

        if healthy:
            # Check configured model
            model_available = client.is_model_available()
            table.add_row(
                "Configured Model",
                "[green]✓[/green]" if model_available else "[yellow]⚠[/yellow]",
                f"{settings.ollama_model} ({'available' if model_available else 'not found - run: plex-rec model pull'})",
            )

            # List available models
            models = client.list_models()
            table.add_row(
                "Available Models",
                str(len(models)),
                ", ".join(models[:5]) + ("..." if len(models) > 5 else "") if models else "None",
            )

        console.print(table)
        client.close()

        if not healthy:
            raise typer.Exit(1)


# Label commands
label_app = typer.Typer(help="Plex label management commands")
app.add_typer(label_app, name="labels")


@label_app.command("apply")
def labels_apply(
    user_id: int | None = typer.Option(None, "--user", "-u", help="Specific user ID"),
):
    """Apply recommendation labels to Plex."""
    service = PlexLabelService()

    if user_id:
        console.print(f"[cyan]Applying labels for user {user_id}...[/cyan]")
        results = service.apply_recommendation_labels(user_id)
    else:
        console.print("[cyan]Applying labels for all users...[/cyan]")
        all_results = service.apply_for_all_users()
        results = {
            "applied": sum(r["applied"] for r in all_results.values()),
            "failed": sum(r["failed"] for r in all_results.values()),
        }

    console.print(f"[green]✓ Applied {results['applied']} labels[/green]")
    if results.get("failed", 0) > 0:
        console.print(f"[yellow]⚠ {results['failed']} labels failed[/yellow]")


@label_app.command("cleanup")
def labels_cleanup(
    all_labels: bool = typer.Option(False, "--all", help="Remove ALL AI labels from library"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Only count labels without removing"),
):
    """Clean up stale recommendation labels."""
    service = PlexLabelService()

    if all_labels:
        console.print("[cyan]Removing all AI recommendation labels...[/cyan]")
        count = service.cleanup_all_ai_labels(dry_run=dry_run)
        action = "Found" if dry_run else "Removed"
        console.print(f"[green]✓ {action} {count} labels[/green]")
    else:
        console.print("[cyan]Removing stale labels...[/cyan]")
        count = service.remove_stale_labels()
        console.print(f"[green]✓ Removed {count} stale labels[/green]")


@label_app.command("stats")
def labels_stats():
    """Show label statistics."""
    service = PlexLabelService()
    stats = service.get_label_stats()

    table = Table(title="Label Statistics")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")

    table.add_row("Total Recommendations", str(stats.get("total", 0)))
    table.add_row("Active", str(stats.get("active", 0)))
    table.add_row("Labels Applied", str(stats.get("labeled", 0)))
    table.add_row("Pending Label", str(stats.get("pending", 0)))

    console.print(table)


# Embeddings commands (RAG)
embeddings_app = typer.Typer(help="Vector embeddings management for RAG-based recommendations")
app.add_typer(embeddings_app, name="embeddings")


@embeddings_app.command("generate")
def embeddings_generate(
    library_id: int | None = typer.Option(
        None, "--library", "-l", help="Specific library section ID to generate embeddings for"
    ),
    batch_size: int = typer.Option(50, "--batch-size", "-b", help="Items per batch"),
    ensure_model: bool = typer.Option(
        False, "--ensure-model", "-e", help="Pull embeddings model if not available"
    ),
):
    """Generate embeddings for library content.

    This creates vector embeddings of all library content (movies, shows) for
    similarity search. This is a prerequisite for RAG-based recommendations.

    Example: plex-rec embeddings generate --ensure-model
    """
    from rich.progress import Progress

    from plex_recommender.embeddings import EmbeddingsService
    from plex_recommender.recommend.engine import OllamaClient

    settings = get_settings()

    # Optionally ensure embeddings model is available
    if ensure_model:
        client = OllamaClient(model=settings.embeddings_model)
        try:
            if not client.check_health():
                console.print("[red]✗ Cannot connect to Ollama[/red]")
                raise typer.Exit(1)

            if not client.is_model_available(settings.embeddings_model):
                console.print(
                    f"[yellow]Pulling embeddings model {settings.embeddings_model}...[/yellow]"
                )
                if not client.pull_model(settings.embeddings_model):
                    console.print("[red]✗ Failed to pull embeddings model[/red]")
                    raise typer.Exit(1)
                console.print("[green]✓ Embeddings model ready[/green]")
        finally:
            client.close()

    console.print("[cyan]Generating embeddings for library content...[/cyan]")
    console.print(f"[dim]Using model: {settings.embeddings_model}[/dim]")
    console.print(f"[dim]Vector store: {settings.lancedb_path}[/dim]")

    service = EmbeddingsService()

    try:
        with Progress() as progress:
            task = progress.add_task("[cyan]Generating embeddings...", total=None)

            def update_progress(current: int, total: int):
                progress.update(task, completed=current, total=total)

            result = service.generate_embeddings(
                library_section_id=library_id,
                batch_size=batch_size,
                progress_callback=update_progress,
            )

        if result["status"] == "success":
            console.print(f"[green]✓ Generated embeddings for {result['processed']} items[/green]")
        elif result["status"] == "no_content":
            console.print("[yellow]⚠ No content found to generate embeddings for[/yellow]")
            console.print("[dim]Run 'plex-rec sync plex' first to populate library content[/dim]")
        else:
            console.print(f"[red]✗ Embedding generation failed: {result}[/red]")
            raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]✗ Embedding generation failed: {e}[/red]")
        raise typer.Exit(1)
    finally:
        service.close()


@embeddings_app.command("status")
def embeddings_status():
    """Show status of the embeddings vector store."""
    from plex_recommender.embeddings import EmbeddingsService

    settings = get_settings()

    console.print("[cyan]Embeddings Configuration[/cyan]")
    console.print(f"  RAG enabled: {settings.use_rag}")
    console.print(f"  Embeddings model: {settings.embeddings_model}")
    console.print(f"  LanceDB path: {settings.lancedb_path}")
    console.print(f"  Top-K retrieval: {settings.rag_top_k}")
    console.print()

    service = EmbeddingsService()
    try:
        stats = service.get_stats()

        vs_stats = stats.get("vector_store", {})
        db_stats = stats.get("database", {})
        coverage = stats.get("coverage", 0)

        table = Table(title="Embeddings Status")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", justify="right")

        table.add_row("Total Embeddings", str(vs_stats.get("total_embeddings", 0)))
        table.add_row("Database Content", str(db_stats.get("total_embeddable", 0)))
        table.add_row("Coverage", f"{coverage:.1f}%")

        if vs_stats.get("embedding_dim"):
            table.add_row("Embedding Dimension", str(vs_stats["embedding_dim"]))

        console.print(table)

        # Show breakdown by library section if available
        lib_sections = vs_stats.get("library_sections", {})
        if lib_sections:
            console.print("\n[dim]Embeddings by Library Section:[/dim]")
            for section_id, count in sorted(lib_sections.items()):
                console.print(f"  Section {section_id}: {count} embeddings")

        if coverage < 100:
            console.print("\n[yellow]⚠ Not all content has embeddings.[/yellow]")
            console.print(
                "[dim]Run 'plex-rec embeddings generate' to generate missing embeddings.[/dim]"
            )
        elif vs_stats.get("total_embeddings", 0) == 0:
            console.print("\n[yellow]⚠ No embeddings found.[/yellow]")
            console.print(
                "[dim]Run 'plex-rec embeddings generate --ensure-model' to get started.[/dim]"
            )

    except Exception as e:
        console.print(f"[red]✗ Failed to get embeddings status: {e}[/red]")
        raise typer.Exit(1)
    finally:
        service.close()


@embeddings_app.command("clear")
def embeddings_clear(
    library_id: int | None = typer.Option(
        None, "--library", "-l", help="Only clear embeddings for this library"
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation"),
):
    """Clear embeddings from the vector store."""
    from plex_recommender.embeddings import VectorStore

    if not force:
        if library_id:
            confirm = typer.confirm(f"Clear embeddings for library {library_id}?")
        else:
            confirm = typer.confirm("Clear ALL embeddings? This cannot be undone.")
        if not confirm:
            raise typer.Abort()

    store = VectorStore()
    try:
        deleted = store.clear(library_section_id=library_id)
        console.print(f"[green]✓ Cleared {deleted} embeddings[/green]")
    except Exception as e:
        console.print(f"[red]✗ Failed to clear embeddings: {e}[/red]")
        raise typer.Exit(1)
    finally:
        store.close()


@embeddings_app.command("search")
def embeddings_search(
    query: str = typer.Argument(..., help="Text query to search for similar content"),
    limit: int = typer.Option(10, "--limit", "-n", help="Number of results"),
    library_id: int | None = typer.Option(
        None, "--library", "-l", help="Filter by library section ID"
    ),
):
    """Search for similar content using vector similarity.

    This is useful for testing the embeddings quality.

    Example: plex-rec embeddings search "sci-fi action movie with robots"
    """
    from plex_recommender.embeddings import VectorStore

    store = VectorStore()
    try:
        console.print(f"[cyan]Searching for content similar to: '{query}'[/cyan]")
        results = store.search_similar(
            query_text=query,
            limit=limit,
            library_section_id=library_id,
        )

        if not results:
            console.print("[yellow]No results found[/yellow]")
            console.print("[dim]Make sure embeddings have been generated.[/dim]")
            return

        table = Table(title=f"Top {len(results)} Similar Items")
        table.add_column("Title", style="cyan")
        table.add_column("Year", justify="right")
        table.add_column("Type")
        table.add_column("Distance", justify="right")

        for result in results:
            distance = result.get("_distance", 0)
            table.add_row(
                result.get("title", "Unknown")[:50],
                str(result.get("year", "")),
                result.get("content_type", ""),
                f"{distance:.4f}",
            )

        console.print(table)
        console.print("\n[dim]Lower distance = more similar[/dim]")
    except Exception as e:
        console.print(f"[red]✗ Search failed: {e}[/red]")
        raise typer.Exit(1)
    finally:
        store.close()


# Scheduler commands
@app.command("serve")
def serve(
    port: int = typer.Option(8000, "--port", "-p", help="Port for API server"),
    host: str = typer.Option("0.0.0.0", "--host", "-h", help="Host to bind to"),
):
    """Start the API server with scheduler."""
    import uvicorn

    from plex_recommender.api import create_app

    console.print(f"[cyan]Starting server on {host}:{port}...[/cyan]")
    app_instance = create_app()
    uvicorn.run(app_instance, host=host, port=port)


@app.command("scheduler")
def run_scheduler():
    """Run the scheduler in standalone mode."""
    import signal
    import sys

    scheduler = JobScheduler()

    def handle_shutdown(signum, frame):
        console.print("\n[yellow]Shutting down scheduler...[/yellow]")
        scheduler.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    console.print("[cyan]Starting scheduler...[/cyan]")
    scheduler.start()

    # Show scheduled jobs
    jobs = scheduler.get_jobs()
    table = Table(title="Scheduled Jobs")
    table.add_column("ID", style="cyan")
    table.add_column("Name")
    table.add_column("Next Run")

    for job in jobs:
        table.add_row(job["id"], job["name"], job["next_run"])

    console.print(table)
    console.print("[green]Scheduler running. Press Ctrl+C to stop.[/green]")

    # Keep running
    signal.pause()


@app.command("config")
def show_config():
    """Show current configuration."""
    settings = get_settings()

    table = Table(title="Configuration")
    table.add_column("Setting", style="cyan")
    table.add_column("Value")

    # Show config (mask sensitive values)
    table.add_row("Plex URL", settings.plex_url)
    table.add_row(
        "Plex Token", "***" + settings.plex_token[-4:] if settings.plex_token else "Not set"
    )
    table.add_row("Tautulli URL", settings.tautulli_url)
    table.add_row(
        "Tautulli API Key",
        "***" + settings.tautulli_api_key[-4:] if settings.tautulli_api_key else "Not set",
    )
    # Mask password in database URL
    db_url_str = str(settings.database_url)
    if "@" in db_url_str and "://" in db_url_str:
        # Extract password from URL and mask it
        import re

        db_url_str = re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", db_url_str)
    table.add_row("Database URL", db_url_str)

    # LLM Provider settings
    table.add_row("", "")  # Separator
    table.add_row("[bold]LLM Settings[/bold]", "")
    table.add_row("LLM Provider", settings.llm_provider)

    if settings.llm_provider == "openrouter":
        table.add_row("OpenRouter Model", settings.openrouter_model)
        table.add_row(
            "OpenRouter API Key",
            "***" + settings.openrouter_api_key[-4:]
            if settings.openrouter_api_key
            else "[red]Not set[/red]",
        )
        table.add_row("Context Window", f"{settings.openrouter_context_window:,} tokens")
    else:
        table.add_row("Ollama URL", settings.ollama_url)
        table.add_row("Ollama Model", settings.ollama_model)
        table.add_row("Context Window", f"{settings.ollama_num_ctx:,} tokens")

    table.add_row("Effective Batch Size", str(settings.get_effective_batch_size()))
    table.add_row("Sync Interval", f"{settings.sync_interval_minutes} minutes")
    table.add_row("Recommendation Day", settings.recommendation_day)
    table.add_row("Recommendation Hour", str(settings.recommendation_hour))
    table.add_row("Min Confidence", f"{settings.min_confidence_score:.0%}")
    table.add_row("Max Recommendations/User", str(settings.max_recommendations_per_user))
    table.add_row("Label Prefix", settings.label_prefix)

    # RAG settings
    table.add_row("", "")  # Separator
    table.add_row("[bold]RAG Settings[/bold]", "")
    table.add_row("Use RAG", str(settings.use_rag))
    table.add_row("Embeddings Model", settings.embeddings_model)
    table.add_row("LanceDB Path", settings.lancedb_path)
    table.add_row("RAG Top-K", str(settings.rag_top_k))

    console.print(table)


if __name__ == "__main__":
    app()
