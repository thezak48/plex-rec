# Plex AI Recommendation Engine

AI-powered recommendation engine that syncs watch history from Tautulli, uses LLMs (Ollama or OpenRouter) to generate personalized recommendations from your Plex library, and applies labels for smart collection display.

## Features

- **Watch History Sync**: Incremental sync from Tautulli with deduplication
- **Library Sync**: Caches Plex metadata for efficient recommendation queries
- **AI Recommendations**: Uses Ollama (local) or OpenRouter (cloud) to generate personalized recommendations with reasoning
- **RAG Support**: Vector similarity search with LanceDB for large libraries
- **Feedback Learning**: Tracks user ratings and watch behavior to improve future recommendations
- **Smart Labels**: Applies namespaced labels (e.g., `AI-Rec:John:High`) to Plex items
- **Scheduling**: Automatic sync (30min) and weekly recommendation generation
- **REST API**: Full API for integration with other tools
- **CLI**: Command-line interface for manual operations

## Requirements

- Python 3.11+
- PostgreSQL 14+
- Plex Media Server
- Tautulli
- **LLM Provider (one of):**
  - Ollama (local) with llama3.2 model
  - OpenRouter API key (cloud, supports Claude, GPT-4, etc.)

## Installation

```bash
cd plex_recommender
pip install -e .
```

## Configuration

Create a `.env` file or set environment variables:

```env
# Required
PLEX_URL=http://localhost:32400
PLEX_TOKEN=your-plex-token
TAUTULLI_URL=http://localhost:8181
TAUTULLI_API_KEY=your-tautulli-api-key

# LLM Provider: 'ollama' (local) or 'openrouter' (cloud)
LLM_PROVIDER=ollama

# Ollama (local LLM)
OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=llama3.2
OLLAMA_NUM_CTX=16384

# OpenRouter (cloud LLM - alternative to Ollama)
# OPENROUTER_API_KEY=sk-or-v1-xxx
# OPENROUTER_MODEL=anthropic/claude-3.5-sonnet
# OPENROUTER_CONTEXT_WINDOW=128000

# Optional (with defaults)
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/plex_recommender
SYNC_INTERVAL_MINUTES=30
RECOMMENDATION_DAY=sunday
RECOMMENDATION_HOUR=3
MIN_CONFIDENCE_SCORE=0.7
MAX_RECOMMENDATIONS_PER_USER=20
LABEL_PREFIX=AI-Rec

# RAG (recommended for large libraries)
USE_RAG=true
EMBEDDINGS_MODEL=nomic-embed-text
RAG_TOP_K=200

# Optional - for keyword enrichment from TMDB
TMDB_API_TOKEN=your-tmdb-read-access-token
```

## Usage

### Initialize Database

```bash
plex-rec db init
```

### Run Migrations

```bash
# Run pending migrations
plex-rec db migrate

# Check migration status
plex-rec db migrate-status
```

### Sync Data

```bash
# Sync watch history from Tautulli
plex-rec sync tautulli

# Sync library from Plex
plex-rec sync plex

# Sync everything
plex-rec sync all

# Enrich library with TMDB keywords (requires TMDB_API_TOKEN)
plex-rec sync tmdb

# Enrich only first 100 items (for testing)
plex-rec sync tmdb --limit 100
```

### Generate Recommendations

```bash
# Generate for all users
plex-rec recommend generate

# Generate for specific user
plex-rec recommend generate --user 1

# Generate for shows instead of movies
plex-rec recommend generate --type show

# List recommendations
plex-rec recommend list --user 1
```

### User Feedback

The system tracks how users respond to recommendations (ratings, watch progress) to improve future suggestions.

```bash
# Collect feedback from Plex ratings and watch history
plex-rec recommend feedback

# Collect for specific user
plex-rec recommend feedback --user 2

# View feedback statistics
plex-rec recommend feedback-stats
plex-rec recommend feedback-stats --user 2
```

Feedback types:
- **loved**: User rated 8+/10 in Plex
- **liked**: User rated 6-8/10
- **completed**: Watched 80%+ of content
- **disliked**: User rated below 6/10
- **skipped**: 30+ days old, never watched

### Manage Labels

```bash
# Apply labels to Plex
plex-rec labels apply

# Clean up stale labels
plex-rec labels cleanup

# Remove ALL AI labels (use with caution)
plex-rec labels cleanup --all

# View label statistics
plex-rec labels stats
```

### Run Server

```bash
# Start API server with scheduler
plex-rec serve

# Run scheduler only (no API)
plex-rec scheduler
```

### View Configuration

```bash
plex-rec config
```

## API Endpoints

- `GET /health` - Health check
- `POST /api/sync/tautulli` - Trigger Tautulli sync
- `POST /api/sync/plex` - Trigger Plex library sync
- `POST /api/recommendations/generate` - Generate recommendations
- `GET /api/recommendations/{user_id}` - Get user recommendations
- `POST /api/labels/apply` - Apply labels to Plex
- `POST /api/labels/cleanup` - Clean up stale labels
- `GET /api/labels/stats` - Get label statistics
- `GET /api/scheduler/jobs` - List scheduled jobs
- `POST /api/scheduler/run/{job_type}` - Manually trigger job
- `GET /api/users` - List all users

## Creating Smart Collections in Plex

After labels are applied, create smart collections in Plex:

1. Go to your library in Plex
2. Click "Collections" → "New Collection"
3. Select "Smart Collection"
4. Add filter: Label → contains → `AI-Rec:YourName:High`
5. Name it "AI Recommendations (High Confidence)"

You can create multiple collections for different confidence tiers.

## Architecture

```
plex_recommender/
├── api.py           # FastAPI application
├── cli.py           # Typer CLI
├── config.py        # Pydantic settings
├── labels.py        # Plex label management
├── logging.py       # Structured logging
├── scheduler.py     # APScheduler jobs
├── db/
│   ├── __init__.py  # Database connection
│   ├── schema.py    # PostgreSQL schema
│   └── migrations/  # Database migrations
├── sync/
│   ├── tautulli.py  # Tautulli sync service
│   ├── plex.py      # Plex sync service
│   └── tmdb.py      # TMDB enrichment
├── recommend/
│   ├── engine.py    # LLM clients (Ollama & OpenRouter)
│   ├── feedback.py  # User feedback collection
│   └── service.py   # Recommendation orchestration
└── embeddings/
    └── service.py   # LanceDB vector embeddings for RAG
```

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Lint
ruff check .
```

## License

MIT
