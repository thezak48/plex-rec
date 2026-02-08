# Contributing to Plex AI Recommendation Engine

Thank you for your interest in contributing! This document provides guidelines for contributing to the project.

## Development Setup

1. **Clone the repository**
   ```bash
   git clone https://github.com/yourusername/plex-ai-recommender.git
   cd plex-ai-recommender
   ```

2. **Create a virtual environment**
   ```bash
   python3.11 -m venv .venv
   source .venv/bin/activate  # Linux/macOS
   # or
   .venv\Scripts\activate     # Windows
   ```

3. **Install dependencies**
   ```bash
   pip install -e ".[dev]"
   ```

4. **Set up pre-commit hooks**
   ```bash
   pre-commit install
   ```

5. **Copy environment file**
   ```bash
   cp .env.example .env
   # Edit .env with your Plex/Tautulli credentials
   ```

## Code Style

This project uses:
- **Ruff** for linting and formatting
- **Type hints** throughout the codebase
- **Pydantic** for data validation

Run linting before committing:
```bash
ruff check .
ruff format .
```

## Project Structure

```
plex_recommender/
├── api.py           # FastAPI REST endpoints
├── cli.py           # Typer CLI commands
├── config.py        # Pydantic settings
├── labels.py        # Plex label management
├── logging.py       # Structured logging (structlog)
├── scheduler.py     # APScheduler background jobs
├── db/
│   ├── __init__.py  # Database connection (psycopg2)
│   ├── schema.py    # PostgreSQL schema
│   └── migrations/  # Versioned migrations
├── sync/
│   ├── tautulli.py  # Tautulli API sync
│   ├── plex.py      # Plex library sync
│   └── tmdb.py      # TMDB enrichment
├── recommend/
│   ├── engine.py    # LLM clients (Ollama/OpenRouter)
│   ├── feedback.py  # User feedback collection
│   └── service.py   # Recommendation orchestration
└── embeddings/
    └── service.py   # LanceDB vector embeddings
```

## Adding a New Feature

1. Create a feature branch: `git checkout -b feature/your-feature`
2. Write your code with type hints
3. Add/update tests if applicable
4. Update documentation (README.md, TECHNICAL.md)
5. Run linting: `ruff check . && ruff format .`
6. Commit with a descriptive message
7. Open a pull request

## Adding a Database Migration

1. Create a new file in `plex_recommender/db/migrations/`:
   ```
   NNN_description.py  (e.g., 003_add_user_preferences.py)
   ```

2. Implement the `upgrade()` function:
   ```python
   """003_add_user_preferences.py - Add user preferences table"""

   from plex_recommender.db import get_db_cursor
   from plex_recommender.logging import get_logger

   logger = get_logger(__name__)

   def upgrade():
       """Apply migration."""
       sql = """
       CREATE TABLE IF NOT EXISTS user_preferences (
           ...
       );
       """
       with get_db_cursor(commit=True) as cursor:
           cursor.execute(sql)
       logger.info("migration_applied", migration="003_add_user_preferences")
   ```

3. Test: `plex-rec db migrate`

## Reporting Issues

When reporting issues, please include:
- Python version (`python --version`)
- Operating system
- Relevant log output
- Steps to reproduce

## Questions?

Feel free to open an issue for questions or discussion.
