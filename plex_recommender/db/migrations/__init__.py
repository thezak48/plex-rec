"""Database migrations system."""

import importlib
import pkgutil
from collections.abc import Callable
from pathlib import Path

from plex_recommender.db import get_db_cursor
from plex_recommender.logging import get_logger

logger = get_logger(__name__)

# Migration table schema
MIGRATIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    id SERIAL PRIMARY KEY,
    version VARCHAR(255) UNIQUE NOT NULL,
    name VARCHAR(255) NOT NULL,
    applied_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
"""


def ensure_migrations_table() -> None:
    """Create the migrations tracking table if it doesn't exist."""
    with get_db_cursor() as cursor:
        cursor.execute(MIGRATIONS_TABLE_SQL)
    logger.debug("migrations_table_ensured")


def get_applied_migrations() -> set[str]:
    """Get set of already applied migration versions."""
    ensure_migrations_table()
    with get_db_cursor(commit=False) as cursor:
        cursor.execute("SELECT version FROM schema_migrations ORDER BY version")
        return {row["version"] for row in cursor.fetchall()}


def record_migration(version: str, name: str) -> None:
    """Record a migration as applied."""
    with get_db_cursor() as cursor:
        cursor.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
            (version, name),
        )


def get_pending_migrations() -> list[tuple[str, str, Callable]]:
    """Get list of pending migrations (version, name, upgrade_func).

    Discovers migration modules in the migrations directory that match
    the pattern NNN_name.py and have an `upgrade()` function.
    """
    applied = get_applied_migrations()
    pending = []

    # Get the migrations package path
    migrations_dir = Path(__file__).parent

    # Find all migration modules
    for module_info in pkgutil.iter_modules([str(migrations_dir)]):
        if module_info.name.startswith("_"):
            continue

        # Parse version from module name (e.g., "001_add_metadata" -> "001")
        parts = module_info.name.split("_", 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue

        version = parts[0]
        name = parts[1]

        if version in applied:
            continue

        # Import the module and get the upgrade function
        try:
            module = importlib.import_module(f".{module_info.name}", package=__name__)
            if hasattr(module, "upgrade"):
                pending.append((version, name, module.upgrade))
            else:
                logger.warning("migration_missing_upgrade", module=module_info.name)
        except Exception as e:
            logger.error("migration_import_failed", module=module_info.name, error=str(e))

    # Sort by version
    pending.sort(key=lambda x: x[0])
    return pending


def run_migrations() -> int:
    """Run all pending migrations.

    Returns:
        Number of migrations applied.
    """
    pending = get_pending_migrations()

    if not pending:
        logger.info("no_pending_migrations")
        return 0

    applied_count = 0
    for version, name, upgrade_func in pending:
        logger.info("applying_migration", version=version, name=name)
        try:
            upgrade_func()
            record_migration(version, name)
            applied_count += 1
            logger.info("migration_applied", version=version, name=name)
        except Exception as e:
            logger.error("migration_failed", version=version, name=name, error=str(e))
            raise

    return applied_count


def get_migration_status() -> list[dict]:
    """Get status of all migrations (applied and pending).

    Returns:
        List of dicts with version, name, status, applied_at.
    """
    ensure_migrations_table()

    # Get applied migrations with timestamps
    with get_db_cursor(commit=False) as cursor:
        cursor.execute("SELECT version, name, applied_at FROM schema_migrations ORDER BY version")
        applied = {row["version"]: row for row in cursor.fetchall()}

    # Get all available migrations
    migrations_dir = Path(__file__).parent
    all_migrations = []

    for module_info in pkgutil.iter_modules([str(migrations_dir)]):
        if module_info.name.startswith("_"):
            continue
        parts = module_info.name.split("_", 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue

        version = parts[0]
        name = parts[1]

        if version in applied:
            all_migrations.append(
                {
                    "version": version,
                    "name": name,
                    "status": "applied",
                    "applied_at": applied[version]["applied_at"],
                }
            )
        else:
            all_migrations.append(
                {
                    "version": version,
                    "name": name,
                    "status": "pending",
                    "applied_at": None,
                }
            )

    all_migrations.sort(key=lambda x: x["version"])
    return all_migrations
