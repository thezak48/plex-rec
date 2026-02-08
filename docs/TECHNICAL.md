# Plex AI Recommendation Engine - Technical Documentation

This document provides a detailed explanation of how the recommendation engine works, from data collection through to final recommendations.

## Table of Contents

1. [System Overview](#system-overview)
2. [Data Flow](#data-flow)
3. [Database Schema](#database-schema)
4. [Database Migrations](#database-migrations)
5. [Recommendation Process](#recommendation-process)
6. [Feedback System](#feedback-system)
7. [LLM Prompt Construction](#llm-prompt-construction)
8. [Configuration Tuning](#configuration-tuning)
9. [Troubleshooting](#troubleshooting)

---

## System Overview

### Architecture Diagram

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│    Tautulli     │     │   PostgreSQL    │     │      Plex       │
│  (Watch Data)   │────▶│   (Storage)     │◀────│   (Library)     │
└─────────────────┘     └────────┬────────┘     └─────────────────┘
                                 │                       │
                                 │              ┌────────┴────────┐
                                 │              │      TMDB       │
                                 │              │ (Enrichment)    │
                                 │              └────────┬────────┘
                                 │                       │
                                 │◀──────────────────────┘
                                 │ Keywords, ratings, languages
                                 │
                                 │ User preferences
                                 │ Watch history
                                 │ Library content
                                 ▼
              ┌──────────────────┴──────────────────┐
              │                                      │
     ┌────────┴────────┐              ┌──────────────┴──────────────┐
     │     Ollama      │      OR      │        OpenRouter           │
     │  (Local LLM)    │              │  (Cloud: Claude, GPT-4...)  │
     └────────┬────────┘              └──────────────┬──────────────┘
              │                                      │
              └──────────────────┬───────────────────┘
                                 │
                                 │ JSON recommendations
                                 ▼
                        ┌─────────────────┐
                        │ Recommendation  │
                        │    Service      │
                        └────────┬────────┘
                                 │
                    ┌────────────┼────────────┐
                    ▼            ▼            ▼
              ┌──────────┐ ┌──────────┐ ┌──────────┐
              │ Database │ │   Plex   │ │   API    │
              │  Save    │ │  Labels  │ │ Response │
              └──────────┘ └──────────┘ └──────────┘
```

### Components

| Component | Purpose | Technology |
|-----------|---------|------------|
| CLI | Command-line interface | Typer |
| API | REST endpoints | FastAPI |
| Scheduler | Background jobs | APScheduler |
| Database | Data persistence | PostgreSQL + psycopg2 |
| Sync Services | Data collection | httpx |
| TMDB Enrichment | Keywords, ratings, languages | TMDB API |
| Recommendation Engine | LLM interaction | Ollama or OpenRouter |
| Vector Store | RAG similarity search | LanceDB |
| Label Manager | Plex integration | plexapi |

---

## Data Flow

### 1. Tautulli Sync (`plex-rec sync tautulli`)

```
Tautulli API ──▶ get_users() ──▶ users table
                                    │
Tautulli API ──▶ get_history() ──▶ watch_history table
                                    │
                              (aggregation)
                                    │
                                    ▼
                            watch_stats table
                                    │
                            user_genre_preferences table
```

**What gets synced:**
- User profiles (username, Plex user ID, email)
- Watch history (title, duration, completion %, platform)
- Aggregated statistics (play count, avg completion per item)
- Genre preferences (computed from watch patterns)

**Incremental sync:** Uses `last_sync_cursor` to only fetch new history since last sync.

### 2. Plex Sync (`plex-rec sync plex`)

```
Plex API ──▶ library.sections() ──▶ library_sections table
                │
                ▼
        For each section:
                │
                ▼
Plex API ──▶ section.all() ──▶ library_content table
```

**What gets synced:**
- Library sections (name, type, ID)
- Content metadata:
  - `plex_rating_key` (unique identifier)
  - `title`, `year`, `summary`
  - `genres` (as array)
  - `rating`, `content_rating`
  - `duration_ms`, `thumb_url`

**Note:** Only movies and shows are synced, not individual episodes.

### 3. TMDB Enrichment (`plex-rec sync tmdb`)

```
library_content ──▶ Find items missing keywords/ratings/languages
        │
        ▼
    For each item:
        │
        ├── Extract IMDB ID from metadata_json
        │
        ▼
TMDB API ──▶ /find/{imdb_id} ──▶ Get TMDB ID, rating, language
        │
        ├── (fallback: search by title/year)
        │
        ▼
TMDB API ──▶ /movie/{id}/keywords ──▶ keywords array
           or /tv/{id}/keywords
        │
        ▼
    UPDATE library_content SET keywords, tmdb_rating, tmdb_id, languages
```

**What gets enriched:**
- `keywords` - Content tags/themes (e.g., "time travel", "heist", "revenge")
- `tmdb_rating` - TMDB vote_average (0-10 scale, more standardized than Plex)
- `tmdb_id` - TMDB ID for future lookups
- `languages` - Original language (converted from ISO 639-1 to full name)

**Matching priority:**
1. IMDB ID (most accurate) - extracted from Plex metadata GUIDs
2. Title + year search (fallback)

**Rate limiting:** Built-in retry with exponential backoff for API limits.

### 4. Recommendation Generation (`plex-rec recommend generate`)

```
┌─────────────────────────────────────────────────────────┐
│                  generate_for_user()                     │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  1. get_user_preferences(user_id)                       │
│     └─▶ Genre affinities, avg completion, etc.          │
│                                                          │
│  2. get_watched_content(user_id, limit)                 │
│     └─▶ Aggregated watch stats from watch_stats table   │
│                                                          │
│  3. get_unwatched_content(user_id, library_id, limit)   │
│     └─▶ Library items NOT in user's watch_stats         │
│                                                          │
│  4. engine.generate_recommendations(...)                 │
│     └─▶ Build prompt, send to Ollama, parse response    │
│                                                          │
│  5. save_recommendations(...)                            │
│     └─▶ Store in recommendations table                   │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

### 5. Feedback Collection (`plex-rec recommend feedback`)

The feedback system tracks how users respond to recommendations, enabling quality measurement and future improvements.

```
┌─────────────────────────────────────────────────────────┐
│                collect_feedback_for_user()               │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  1. Get pending recommendations (active, no feedback)    │
│                                                          │
│  2. Check watch_stats for each recommendation:           │
│     ├─▶ avg_completion >= 80% → "completed"             │
│     └─▶ any watch activity    → "watched"               │
│                                                          │
│  3. Check Plex userRating (0-10):                       │
│     ├─▶ rating >= 8.0 → "loved"                         │
│     ├─▶ rating >= 6.0 → "liked"                         │
│     └─▶ rating <  6.0 → "disliked"                      │
│                                                          │
│  4. Check recommendation age:                            │
│     └─▶ > 30 days old + unwatched → "skipped"           │
│                                                          │
│  5. Update recommendations.user_feedback                 │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

#### Feedback Values

| Feedback | Meaning | Detection |
|----------|---------|-----------|
| `loved` | User rated highly | Plex userRating ≥ 8.0 |
| `liked` | User rated positively | Plex userRating ≥ 6.0 |
| `completed` | User finished watching | avg_completion ≥ 80% |
| `watched` | User started watching | Any watch activity |
| `disliked` | User rated negatively | Plex userRating < 6.0 |
| `skipped` | User ignored | 30+ days old, never watched |

#### Feedback Priority

Explicit feedback (ratings) takes priority over implicit feedback (watch activity):

1. **User rating** (if available) → loved/liked/disliked
2. **Watch completion** → completed/watched
3. **Age + no activity** → skipped

#### Quality Metrics

```bash
# View feedback statistics
plex-rec recommend feedback-stats

# Example output:
# ❤️ loved: 12
# 👍 liked: 8
# ✅ completed: 45
# 👁️ watched: 23
# 👎 disliked: 3
# ⏭️ skipped: 18
# 
# Hit Rate: 59.6%  (positive / total with feedback)
```

#### Running Feedback Collection

```bash
# Collect feedback for all users
plex-rec recommend feedback

# Collect for specific user
plex-rec recommend feedback --user 44

# View statistics only
plex-rec recommend feedback --stats
```

Feedback is also collected automatically during the weekly recommendation job.

---

## Database Schema

### Entity Relationship

```
users (1) ◀──────────────────────▶ (N) watch_history
  │                                        │
  │                                        │
  ▼                                        ▼
(1) ◀─────────────────────────────▶ (N) watch_stats
  │                                        │
  │                                        │
  ▼                                        │
(1) ◀── user_genre_preferences (N)         │
  │                                        │
  │                                        │
  ▼                                        ▼
(1) ◀─────── recommendations (N)    library_content
                                           │
                                           │
                                           ▼
                                    library_sections
```

### Key Tables

#### users
| Column | Type | Description |
|--------|------|-------------|
| id | SERIAL | Internal auto-increment ID (used in foreign keys) |
| plex_user_id | VARCHAR | Tautulli/Plex user ID (external reference) |
| username | VARCHAR | Display name |

#### watch_stats (Aggregated per user/content)
| Column | Type | Description |
|--------|------|-------------|
| user_id | INTEGER | FK to users.id |
| plex_rating_key | VARCHAR | Content identifier |
| total_play_count | INTEGER | Times watched |
| avg_completion_percent | DECIMAL | Average % watched |
| last_watched_at | TIMESTAMP | Most recent watch |

#### library_content
| Column | Type | Description |
|--------|------|-------------|
| plex_rating_key | VARCHAR | Unique Plex identifier |
| library_section_id | INTEGER | Which library it's in |
| content_type | VARCHAR | 'movie' or 'show' |
| title | VARCHAR | Display title |
| genres | TEXT[] | Array of genre names |
| actors | TEXT[] | Array of actor names (top 10) |
| keywords | TEXT[] | Array of keywords from TMDB |
| languages | TEXT[] | Array of languages (from TMDB) |
| rating | DECIMAL | Plex audience/critic rating |
| tmdb_rating | DECIMAL | TMDB vote_average (0-10) |
| tmdb_id | INTEGER | TMDB ID for caching lookups |

#### recommendations
| Column | Type | Description |
|--------|------|-------------|
| user_id | INTEGER | FK to users.id |
| plex_rating_key | VARCHAR | Recommended content |
| library_section_id | INTEGER | Which library the recommendation is for |
| confidence_score | DECIMAL | 0.00 to 1.00 |
| reasoning | TEXT | LLM explanation |
| recommendation_factors | JSONB | Contributing factors |
| is_active | BOOLEAN | Current recommendation |
| user_feedback | VARCHAR | Feedback: loved, liked, completed, watched, disliked, skipped |
| feedback_at | TIMESTAMP | When feedback was recorded |

### User ID Correlation

The internal `users.id` is used for all foreign keys. To map between systems:

```sql
-- Find user by Plex ID
SELECT id FROM users WHERE plex_user_id = '7142032';

-- See who watched what
SELECT u.username, ws.total_play_count, lc.title
FROM watch_stats ws
JOIN users u ON ws.user_id = u.id
JOIN library_content lc ON ws.plex_rating_key = lc.plex_rating_key;
```

---

## Database Migrations

The system uses a versioned migration system to manage schema changes safely.

### Migration System Architecture

```
plex_recommender/db/migrations/
├── __init__.py              # Core migration functions
├── 001_add_metadata_columns.py
├── 002_future_migration.py
└── ...
```

### schema_migrations Table

Tracks which migrations have been applied:

| Column | Type | Description |
|--------|------|-------------|
| id | SERIAL | Auto-increment ID |
| version | VARCHAR | Migration version (e.g., "001") |
| name | VARCHAR | Migration name (e.g., "add_metadata_columns") |
| applied_at | TIMESTAMP | When migration was applied |

### Creating a New Migration

1. Create a new file in `plex_recommender/db/migrations/`:
   - Format: `NNN_migration_name.py` (e.g., `002_add_indexes.py`)
   - NNN must be a unique, ascending number

2. Implement the `upgrade()` function (required):

```python
"""002_add_indexes.py - Add performance indexes"""

from plex_recommender.db import get_db_cursor
from plex_recommender.logging import get_logger

logger = get_logger(__name__)

def upgrade():
    """Apply migration."""
    sql = """
    CREATE INDEX IF NOT EXISTS idx_example ON table_name(column_name);
    """
    with get_db_cursor() as cursor:
        cursor.execute(sql)
    logger.info("migration_002_applied")

def downgrade():
    """Reverse migration (optional)."""
    sql = "DROP INDEX IF EXISTS idx_example;"
    with get_db_cursor() as cursor:
        cursor.execute(sql)
```

3. Run migrations:

```bash
plex-rec db migrate
```

### Migration Commands

```bash
# Run all pending migrations
plex-rec db migrate

# Check migration status
plex-rec db migrate-status
```

### Example Migrations

#### 001_add_metadata_columns

Adds columns for actor, keyword, and language data:

```sql
ALTER TABLE library_content ADD COLUMN IF NOT EXISTS actors TEXT[];
ALTER TABLE library_content ADD COLUMN IF NOT EXISTS keywords TEXT[];
ALTER TABLE library_content ADD COLUMN IF NOT EXISTS languages TEXT[];
```

After running, re-sync Plex: `plex-rec sync plex`

#### 002_add_tmdb_rating

Adds TMDB rating and ID columns for enhanced metadata:

```sql
ALTER TABLE library_content ADD COLUMN IF NOT EXISTS tmdb_rating DECIMAL(3, 1);
ALTER TABLE library_content ADD COLUMN IF NOT EXISTS tmdb_id INTEGER;
```

After running, enrich with TMDB: `plex-rec sync tmdb`

### Auto-Initialization

The database is automatically initialized when needed:

- **API startup**: Checks if DB exists, creates schema and runs migrations if not
- **CLI commands**: Any non-`db` command (e.g., `sync`, `recommend`) will auto-initialize
- **`db init`**: Explicitly creates schema AND runs all pending migrations

This means you can skip `plex-rec db init` entirely - just run `plex-rec sync all` and it will set everything up automatically.

---

## Recommendation Process

### Step 1: Compute User Preferences

```python
preferences = {
    "genres": {
        "Drama": 0.89,      # Weighted by watch time × completion
        "Comedy": 0.76,
        "Sci-Fi": 0.65
    },
    "preferred_content_rating": "TV-MA",  # Most watched rating
    "avg_completion": 85.5,               # Average completion %
    "peak_viewing_time": "Evening"        # When they watch
}
```

**Genre Affinity Formula:**
```
affinity = (total_watch_time_seconds × avg_completion_percent) / max_score
```

### Step 2: Gather Watch History

From `watch_stats` table (aggregated, not individual events):

```python
watched_content = [
    {
        "plex_rating_key": "12345",
        "title": "Breaking Bad",
        "year": 2008,
        "genres": ["Drama", "Crime", "Thriller"],
        "play_count": 3,          # Total times watched
        "avg_completion": 95.5    # Average completion %
    },
    ...
]
```

### Step 3: Get Unwatched Library Content

Query library_content excluding items in user's watch_stats:

```sql
SELECT lc.* FROM library_content lc
LEFT JOIN watch_stats ws ON lc.plex_rating_key = ws.plex_rating_key 
    AND ws.user_id = ?
WHERE ws.id IS NULL                    -- Not watched
  AND lc.library_section_id = ?        -- Optional: specific library
ORDER BY lc.rating DESC
LIMIT ?                                -- MAX_LIBRARY_ITEMS setting
```

### Step 4: LLM Generation

The prompt and response flow:

```
┌─────────────────────────────────────────────────────┐
│                   SYSTEM PROMPT                      │
│  - Role: Recommendation expert                       │
│  - Output format: JSON schema                        │
│  - Rules: Use rating_key from list, be specific     │
│  - Importance Weights:                               │
│      • Genre: 25%      • Actor: 20%                 │
│      • Keyword: 25%    • Studio: 15%                │
│      • Language: 10%   • Year: 5%                   │
└─────────────────────────────────────────────────────┘
                        +
┌─────────────────────────────────────────────────────┐
│                    USER PROMPT                       │
│  - Watch history with play counts                   │
│  - Genre preferences with affinity scores           │
│  - Available unwatched content list                 │
│  - Instructions with example using real rating_key  │
└─────────────────────────────────────────────────────┘
                        │
                        ▼
                   ┌─────────┐
                   │ Ollama  │
                   │   LLM   │
                   └────┬────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────┐
│                  JSON RESPONSE                       │
│  {                                                   │
│    "recommendations": [                              │
│      {                                               │
│        "rating_key": "2204943",                     │
│        "title": "Better Call Saul",                 │
│        "confidence": 0.92,                          │
│        "reasoning": "You watched Breaking Bad 3x...",│
│        "matching_factors": ["Drama", "Crime", ...]  │
│      }                                               │
│    ]                                                 │
│  }                                                   │
└─────────────────────────────────────────────────────┘
```

### Step 5: Validation & Saving

Before saving, recommendations are validated:

1. **Parse JSON** - Handle malformed responses
2. **Validate rating_key** - Must exist in available content
3. **Check confidence** - Must be ≥ `MIN_CONFIDENCE_SCORE`
4. **Save to database** - With 7-day expiration

---

## Feedback System

The feedback system tracks how users respond to recommendations, improving future suggestions by learning from past successes and failures.

### Feedback Types

| Feedback | Detection Method | Signal |
|----------|------------------|--------|
| `loved` | Plex userRating ≥ 8.0 | Strong positive |
| `liked` | Plex userRating 6.0-7.9 | Positive |
| `completed` | Watched ≥80% completion | Implicit positive |
| `watched` | Any watch history | Weak positive |
| `disliked` | Plex userRating < 6.0 | Negative |
| `skipped` | 30+ days old, never watched | Weak negative |

### Feedback Flow

```
┌─────────────────────────────────────────────────────┐
│              Plex Media Server                       │
│  - User rates item (0-10 stars)                     │
│  - Watch progress tracked                            │
└─────────────────┬───────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────┐
│           Feedback Collection Job                    │
│  (Weekly, after recommendation generation)          │
│                                                      │
│  For each recommendation without feedback:          │
│  1. Query Plex API for userRating                   │
│  2. Check watch_stats for completion                │
│  3. Calculate days since recommendation             │
│  4. Determine feedback type by priority:            │
│     - Rated < 6.0 → disliked                        │
│     - Rated ≥ 8.0 → loved                           │
│     - Rated 6-8 → liked                             │
│     - Watched ≥80% → completed                      │
│     - Any watch → watched                           │
│     - 30+ days old → skipped                        │
└─────────────────┬───────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────┐
│              recommendations table                   │
│  user_feedback = 'loved' | 'liked' | 'disliked'... │
│  feedback_at = timestamp                             │
└─────────────────────────────────────────────────────┘
```

### Feedback in LLM Prompts

When generating new recommendations, the system includes feedback history in the prompt:

```
### Previous Recommendations User LOVED (rated 8+/10):
  - Chernobyl (2019) [Drama, History, Thriller]
  - The Bear (2022) [Comedy, Drama]

### Previous Recommendations User LIKED (rated 6-8/10):
  - Severance (2022) [Drama, Mystery, Sci-Fi]

### Previous Recommendations User DISLIKED (avoid similar):
  - Reality TV Show (2023) [Reality]

### Previous Recommendations User IGNORED (30+ days, never watched):
  - Old Movie (2015) [Action]
```

The LLM is instructed to:
- **PRIORITIZE** content similar to items the user LOVED
- **AVOID** content similar to items the user DISLIKED or SKIPPED

### CLI Commands

```bash
# Collect feedback for all users
plex-rec recommend feedback

# Collect feedback for specific user
plex-rec recommend feedback --user 2

# View feedback statistics
plex-rec recommend feedback-stats
plex-rec recommend feedback-stats --user 2
```

### Feedback Statistics Example

```
Feedback Statistics for user 2:

  loved:     3 recommendations (23%)
  liked:     2 recommendations (15%)
  completed: 1 recommendations (8%)
  watched:   1 recommendations (8%)
  disliked:  0 recommendations (0%)
  skipped:   4 recommendations (31%)
  pending:   2 recommendations (15%)
  ─────────────────────────────────
  total:    13 recommendations
```

---

## LLM Prompt Construction

### What's Sent for Watch History

```
### Recently Watched Content (showing patterns):
- Breaking Bad (2008) [Drama, Crime, Thriller] - watched 3x, 95% complete
- The Office (2005) [Comedy] - watched 12x, 78% complete
- Stranger Things (2016) [Drama, Sci-Fi, Horror] - watched 2x, 100% complete
```

**Fields:**
- Title and year
- First 3 genres
- Play count (from aggregated watch_stats)
- Average completion percentage

### What's Sent for Library Items

**Compact format (default):**
```
## Available Unwatched Content (500 items)
Format: [KEY] Title (Year) G:genres C:cast S:studio L:lang K:keywords R:rating
(G=Genre, C=Cast, S=Studio, L=Language, K=Keywords, R=Rating)

[2204943] Better Call Saul (2015) G:Dra,Cri C:Bob,Rhea S:AMC L:En K:lawyer,crime R:8.9
[1958234] Parks and Recreation (2009) G:Com C:Amy,Nick S:NBC L:En K:mockument R:8.6
[2301847] Dark (2017) G:Dra,Sci C:Louis S:Netflix L:De K:time tra R:8.8
```

**Verbose format (`COMPACT_PROMPT=false`):**
```
## Available Unwatched Content (200 items)
Format: [RATING_KEY] Title (Year) | Genres | Cast | Studio | Lang | Tags | Rating

- [2204943] Better Call Saul (2015) | Genres: Drama, Crime | Cast: Bob Odenkirk, Rhea Seehorn | Studio: AMC | Lang: English | Tags: lawyer, crime | TMDB: 8.9/10
```

**Fields (for weight-based recommendations):**
- Rating key (in brackets for LLM to copy)
- Title and year
- Genres (compact: 2 items × 3 chars, verbose: 3 items full)
- Actors/Cast (compact: 2 first names, verbose: 3 full names)
- Studio/Network (compact: 12 chars max)
- Language (compact: 2 chars, verbose: full name)
- Keywords/Tags (compact: 2 × 8 chars, verbose: 3 full)
- TMDB Rating (preferred) or Plex Rating (fallback)

The recommendation service orders unwatched content by `COALESCE(tmdb_rating, rating)` to prefer TMDB's standardized 0-10 ratings when available.

These fields correspond to the configurable weights in the system prompt, allowing the LLM to make better recommendations based on your preference priorities.

### Token Optimization

The system supports two prompt formats controlled by `COMPACT_PROMPT` (default: `true`).

#### Compact Format (Default)

```
[2204943] Better Call Saul (2015) G:Dra,Cri C:Bob,Rhea S:AMC L:En K:lawyer,crime R:8.9
```

**Optimizations:**
- Single-letter labels (G: C: S: L: K: R:)
- Genres truncated to 3 characters (Drama → Dra)
- Actors: first name only (Bob Odenkirk → Bob)
- Languages: 2 characters (English → En)
- Keywords: max 8 characters each
- 2 items max per category instead of 3
- No pipes or dashes

#### Verbose Format

```
- [2204943] Better Call Saul (2015) | Genres: Drama, Crime | Cast: Bob Odenkirk, Rhea Seehorn | Studio: AMC | Lang: English | Tags: lawyer, crime | TMDB: 8.9/10
```

To use verbose format: `COMPACT_PROMPT=false`

#### Token Savings Comparison

| Library Items | Verbose Tokens | Compact Tokens | Savings |
|---------------|----------------|----------------|----------|
| 200 items | ~10,400 | ~4,000 | ~6,400 (62%) |
| 500 items | ~26,000 | ~10,000 | ~16,000 (62%) |
| 1000 items | ~52,000 | ~20,000 | ~32,000 (62%) |

#### Total Prompt Size (Compact Mode)

| Content | Approximate Tokens |
|---------|-------------------|
| System prompt | ~500 tokens |
| Watch history (100 items) | ~2,500 tokens |
| Library (500 items) | ~10,000 tokens |
| Instructions | ~300 tokens |
| **Total** | **~13,300 tokens** |

With `OLLAMA_NUM_CTX=32768` and compact mode, you can send **~1,500 library items** while leaving room for response generation.

**Note:** With verbose format, the same context window fits only ~500 library items.

---

## Configuration Tuning

### LLM Provider Selection

The recommendation engine supports two LLM backends:

| Provider | Type | Context Window | Best For |
|----------|------|----------------|----------|
| **Ollama** | Local | 4K-128K (model dependent) | Privacy, no API costs, GPU owners |
| **OpenRouter** | Cloud | Up to 200K+ | Large libraries, powerful models, no GPU |

#### Ollama (Local)

```dotenv
LLM_PROVIDER=ollama
OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=llama3.2
OLLAMA_NUM_CTX=16384
OLLAMA_TIMEOUT_SECONDS=300
```

**Recommended models:**
- `llama3.2` - Good balance of quality and speed
- `llama3.1:70b` - Higher quality, requires more VRAM
- `qwen2.5:32b` - Excellent for recommendations

#### OpenRouter (Cloud)

```dotenv
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-v1-xxx
OPENROUTER_MODEL=anthropic/claude-3.5-sonnet
OPENROUTER_CONTEXT_WINDOW=128000
```

**Recommended models:**
- `anthropic/claude-3.5-sonnet` - Excellent reasoning, 200K context
- `google/gemini-pro-1.5` - Good value, 1M context
- `openai/gpt-4-turbo` - High quality, 128K context
- `deepseek/deepseek-chat` - Budget option, good quality

**Note:** OpenRouter automatically calculates batch sizes based on context window. With 128K+ context, you can often send 3000+ library items in a single call.

#### Checking LLM Status

```bash
# Check configured provider and connectivity
plex-rec model check

# For Ollama - pull a model
plex-rec model pull --model llama3.2
```

### Settings Impact

| Setting | Impact | Default |
|---------|--------|---------|
| `LLM_PROVIDER` | Which LLM backend to use | `ollama` |
| `MAX_LIBRARY_ITEMS` | Limit items per call (0 = use batching) | 0 (all) |
| `MAX_WATCH_HISTORY_ITEMS` | More = better preference understanding | 0 (all) |
| `COMPACT_PROMPT` | Reduces tokens by ~62%, allows more library items | true |
| `BATCH_PROCESSING` | Process large libraries in batches | true |
| `BATCH_SIZE` | Items per batch (0 = auto-calculate) | 0 |
| `OLLAMA_NUM_CTX` | Ollama context window | 16384 |
| `OPENROUTER_CONTEXT_WINDOW` | OpenRouter context window | 128000 |
| `OLLAMA_TIMEOUT_SECONDS` | Timeout for LLM requests | 300 |
| `MIN_CONFIDENCE_SCORE` | Lower = more recommendations saved | 0.7 |
| `TMDB_API_TOKEN` | Required for TMDB enrichment | None |

### TMDB Configuration

To enable TMDB enrichment, you need a TMDB API Read Access Token:

1. Create a free account at [themoviedb.org](https://www.themoviedb.org/)
2. Go to Settings → API → Create → Read Access Token
3. Add to your `.env`:

```dotenv
TMDB_API_TOKEN=eyJhbGciOiJIUzI1NiJ9...
```

TMDB enrichment is **optional** but recommended. Without it:
- `keywords` will be empty (Plex labels are user tags, not content keywords)
- `languages` may be incomplete (Plex audio tracks vs TMDB original language)
- Ratings will only use Plex's less standardized ratings

### Recommendation Weights

The LLM uses configurable weights to prioritize different matching factors when calculating confidence scores. Weights should sum to 1.0.

| Setting | Purpose | Default |
|---------|---------|---------|
| `WEIGHT_GENRE` | Genre matching importance | 0.25 |
| `WEIGHT_ACTOR` | Actor/cast matching | 0.20 |
| `WEIGHT_KEYWORD` | Keywords/themes matching | 0.25 |
| `WEIGHT_STUDIO` | Studio/network preference | 0.15 |
| `WEIGHT_LANGUAGE` | Language preference | 0.10 |
| `WEIGHT_YEAR` | Release year proximity | 0.05 |

**Example configurations:**

For users who love specific actors:
```dotenv
WEIGHT_ACTOR=0.35
WEIGHT_GENRE=0.20
WEIGHT_KEYWORD=0.20
WEIGHT_STUDIO=0.10
WEIGHT_LANGUAGE=0.10
WEIGHT_YEAR=0.05
```

For users who primarily watch foreign language content (e.g., Korean dramas, anime):
```dotenv
WEIGHT_LANGUAGE=0.25
WEIGHT_GENRE=0.25
WEIGHT_KEYWORD=0.20
WEIGHT_ACTOR=0.15
WEIGHT_STUDIO=0.10
WEIGHT_YEAR=0.05
```

The weights are included in the system prompt to guide the LLM's confidence scoring.

### Batch Processing

For libraries larger than the context window can handle, the system automatically processes content in batches:

```
Library: 5000 movies
Batch size: 500
─────────────────────────────────────
Batch 1/10: items 1-500 → 3 recommendations
Batch 2/10: items 501-1000 → 2 recommendations
...
Batch 10/10: items 4501-5000 → 4 recommendations
─────────────────────────────────────
Total found: 28 recommendations
Kept: top 20 by confidence score
```

**How it works:**
1. Fetches ALL unwatched content (no limit)
2. If `BATCH_PROCESSING=true` and items > `BATCH_SIZE`, splits into batches
3. Runs LLM for each batch separately
4. Collects and deduplicates recommendations from all batches
5. Sorts by confidence and keeps top N (`MAX_RECOMMENDATIONS_PER_USER`)

**When to use batching vs limits:**

| Scenario | Configuration |
|----------|---------------|
| Large library, want full coverage | `MAX_LIBRARY_ITEMS=0` `BATCH_PROCESSING=true` |
| Fast single-call, accept partial coverage | `MAX_LIBRARY_ITEMS=500` `BATCH_PROCESSING=false` |
| Small library (<500 items) | Either works, single call is faster |

### RAG (Retrieval-Augmented Generation)

**RAG is the recommended approach for large libraries (1000+ items).** Instead of batching through the entire library, RAG uses vector similarity search to find the most relevant content based on the user's watch history, then sends only those items to the LLM.

#### How RAG Works

```
┌─────────────────────────────────────────────────────────────────────┐
│                    RAG-Based Recommendation Flow                     │
└─────────────────────────────────────────────────────────────────────┘

┌──────────────┐      One-time Setup (plex-rec embeddings generate)
│   Library    │──────────────────────────────────────────────────────┐
│   Content    │                                                       │
│  (5000 items)│                                                       │
└──────┬───────┘                                                       │
       │                                                               ▼
       │  Generate text representation                     ┌───────────────────┐
       │  Title + Genres + Summary + Keywords + Cast       │   LanceDB Vector  │
       ├──────────────────────────────────────────────────▶│      Store        │
       │                                                   │ (embeddings.lance)│
       │  Ollama embeddings API (nomic-embed-text)         └────────┬──────────┘
       │                                                            │
       └────────────────────────────────────────────────────────────┘

┌──────────────┐      At Recommendation Time
│    User's    │
│Watch History │
│  (100 items) │
└──────┬───────┘
       │
       │  Create weighted query embedding
       │  (higher weight for completed, recent watches)
       │
       ▼
┌──────────────────────┐           ┌────────────────────┐
│   Vector Similarity  │  Top 200  │   LLM Provider     │
│      Search          │──────────▶│ (Ollama/OpenRouter)│
│   (LanceDB kNN)      │  relevant └────────┬───────────┘
└──────────────────────┘  items             │
                                             │ JSON recommendations
                                             ▼
                                  ┌──────────────────────┐
                                  │   Final 20 Recs      │
                                  │   (by confidence)    │
                                  └──────────────────────┘
```

#### Benefits of RAG

| Approach | Items Processed | LLM Calls | Time for 5000 Items |
|----------|-----------------|-----------|---------------------|
| Batching (500/batch) | All 5000 | 10 calls | ~5-10 minutes |
| RAG (top 200) | 200 most relevant | 1 call | ~30 seconds |

**Additional benefits:**
- **Better quality**: Only relevant items reach the LLM, reducing noise
- **Lower VRAM usage**: Single smaller prompt fits easily in context
- **Consistent latency**: Always processes similar number of items
- **Personalized selection**: Vector search uses watch history patterns

#### RAG with OpenRouter (Recommended Hybrid Setup)

RAG works with OpenRouter by using Ollama only for lightweight embeddings generation, while the expensive LLM reasoning is handled by cloud models with large context windows.

```
┌─────────────────┐                    ┌─────────────────┐
│  Ollama Local   │ ──(embeddings)──▶  │    LanceDB      │
│ nomic-embed-text│                    │ Vector Search   │
└─────────────────┘                    └────────┬────────┘
                                                │
                                          Top 200 items
                                                │
                                                ▼
                                       ┌─────────────────┐
                                       │   OpenRouter    │
                                       │ (Claude, GPT-4) │
                                       └─────────────────┘
```

**Configuration:**
```dotenv
# LLM for recommendations (cloud)
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-v1-xxx
OPENROUTER_MODEL=anthropic/claude-3.5-sonnet
OPENROUTER_CONTEXT_WINDOW=128000

# RAG uses Ollama for embeddings only (local, lightweight)
USE_RAG=true
OLLAMA_URL=http://localhost:11434
EMBEDDINGS_MODEL=nomic-embed-text
RAG_TOP_K=200
```

**Setup:**
```bash
# Pull the lightweight embeddings model (one-time)
plex-rec model pull --model nomic-embed-text

# Generate embeddings (one-time, ~10 min for 5000 items)
plex-rec embeddings generate --ensure-model

# Generate recommendations (uses OpenRouter)
plex-rec recommend generate --user 44
```

**Benefits of this hybrid approach:**
- **Best of both worlds**: Fast local embeddings + powerful cloud LLM
- **No GPU required for LLM**: Only need basic Ollama for embeddings
- **Cost efficient**: Embeddings are free (local), only pay for LLM calls
- **Smaller prompts**: RAG sends only ~200 relevant items instead of thousands

#### RAG Configuration

| Setting | Purpose | Default |
|---------|---------|---------|
| `USE_RAG` | Enable RAG-based retrieval | `true` |
| `LANCEDB_PATH` | Path to vector database | `./data/lancedb` |
| `EMBEDDINGS_MODEL` | Ollama model for embeddings | `nomic-embed-text` |
| `RAG_TOP_K` | Number of items to retrieve | `200` |

**Example `.env`:**
```dotenv
USE_RAG=true
LANCEDB_PATH=./data/lancedb
EMBEDDINGS_MODEL=nomic-embed-text
RAG_TOP_K=200
```

#### Setting Up RAG

1. **Pull the embeddings model:**
   ```bash
   plex-rec model pull --model nomic-embed-text
   ```

2. **Generate embeddings for library content:**
   ```bash
   plex-rec embeddings generate --ensure-model
   ```

3. **Check embeddings status:**
   ```bash
   plex-rec embeddings status
   ```

4. **Generate recommendations (RAG is used automatically):**
   ```bash
   plex-rec recommend generate --user 44
   ```

#### Embeddings CLI Commands

```bash
# Generate embeddings (with progress bar)
plex-rec embeddings generate

# Generate for specific library only
plex-rec embeddings generate --library 8

# Ensure model is pulled before generating
plex-rec embeddings generate --ensure-model

# Check status and coverage
plex-rec embeddings status

# Test similarity search
plex-rec embeddings search "sci-fi action movie with robots"

# Clear all embeddings
plex-rec embeddings clear --force

# Clear embeddings for one library
plex-rec embeddings clear --library 8
```

#### Embedding Generation Details

Each library item is converted to a text representation for embedding:

```
The Matrix (1999) | Genres: Action, Sci-Fi | A computer hacker learns from 
mysterious rebels about the true nature of his reality... | Tags: virtual 
reality, dystopia, artificial intelligence | Cast: Keanu Reeves, Laurence 
Fishburne | Studio: Warner Bros | Language: English
```

The text includes:
- Title and year
- Genres (all)
- Summary/description (truncated to 500 chars)
- TMDB keywords (top 10)
- Top actors (5)
- Studio
- Language

This rich representation enables semantic matching - a user who watches "Matrix" will get recommendations for other cyberpunk/dystopian sci-fi, even if the genre tags aren't exact matches.

#### When RAG Falls Back to Batching

RAG automatically falls back to batch processing if:
- No embeddings exist for the library
- The embeddings model is unavailable
- Vector search returns no results

To force non-RAG mode: `USE_RAG=false`

#### Updating Embeddings

Embeddings should be regenerated when:
- New content is added to the library (`plex-rec sync plex`)
- TMDB enrichment adds new keywords (`plex-rec sync tmdb`)

**Recommended workflow:**
```bash
# Full sync with auto-embedding generation
plex-rec sync all

# Or manually regenerate embeddings
plex-rec sync plex
plex-rec sync tmdb
plex-rec embeddings generate
```

The `sync all` command automatically generates/updates embeddings when `USE_RAG=true`.

### Recommended Configurations

**Small Library (<500 items), Good GPU (16GB+ VRAM):**
```dotenv
MAX_LIBRARY_ITEMS=0           # Send everything in one call
BATCH_PROCESSING=false        # No need to batch
OLLAMA_NUM_CTX=65536          # 64K context
OLLAMA_TIMEOUT_SECONDS=300
```

**Large Library (2000+ items), Batch Processing:**
```dotenv
MAX_LIBRARY_ITEMS=0           # Enable batching
BATCH_PROCESSING=true
BATCH_SIZE=500                # 500 for compact, 200 for verbose
COMPACT_PROMPT=true
OLLAMA_NUM_CTX=32768          # 32K context fits 500 compact items
OLLAMA_TIMEOUT_SECONDS=600    # More time for multiple batches
```

**Large Library, Fast Single-Call (partial coverage):**
```dotenv
MAX_LIBRARY_ITEMS=500         # Only see top 500 rated items
BATCH_PROCESSING=false
COMPACT_PROMPT=true
OLLAMA_NUM_CTX=32768
```

**Very Large Library, Run by Library:**
```bash
# Generate per-library to keep prompts small
plex-rec recommend generate --user 44 --library 8   # Anime
plex-rec recommend generate --user 44 --library 14  # Movies
plex-rec recommend generate --user 44 --library 3   # TV Shows
```

---

## Troubleshooting

### Common Issues

#### 1. "Truncating input prompt"

**Ollama log:**
```
level=WARN msg="truncating input prompt" limit=4096 prompt=71828
```

**Cause:** Prompt exceeds context window
**Fix:** Increase `OLLAMA_NUM_CTX` or reduce `MAX_LIBRARY_ITEMS`

#### 2. Ollama Timeout/Crash (500 Error)

**Cause:** Out of VRAM with large context window
**Fix:** 
```dotenv
OLLAMA_NUM_CTX=16384    # Reduce from 131072
MAX_LIBRARY_ITEMS=100   # Send fewer items
```

#### 3. "Generated X recommendations" but "Saved 0"

**Cause 1:** Confidence scores below threshold
```
2026-02-02 [info] recommendations_generated count=5
2026-02-02 [info] recommendations_saved count=0
```
**Fix:** Lower `MIN_CONFIDENCE_SCORE=0.5`

**Cause 2:** Invalid rating keys (LLM made them up)
```
2026-02-02 [warning] all_recommendations_filtered_out
   sample_returned_keys=['12345', '67890']
   sample_valid_keys=['2204943', '1958234']
```
**Fix:** Check that LLM is using keys from the provided list (prompt may need adjustment)

#### 4. "No unwatched content"

**Cause:** Content type mismatch
```bash
# This fails if library 8 is TV shows:
plex-rec recommend generate --user 44 --type movie --library 8
```
**Fix:** Omit `--type` when using `--library`:
```bash
plex-rec recommend generate --user 44 --library 8
```

#### 5. LLM Returns Placeholder Text

**Response:**
```json
{"reasoning": "Explanation of why user would enjoy this"}
```

**Cause:** LLM copying example instead of generating
**Fix:** System prompt has been updated to emphasize specific reasoning

#### 6. OpenRouter Context Length Exceeded

**Error:**
```
openrouter_api_error code=502 message="The input (218031 tokens) is longer than the model's context length (163840 tokens)"
```

**Cause:** Library too large for model's context window
**Fix options:**
1. Enable RAG to pre-filter content: `USE_RAG=true`
2. Limit library items: `MAX_LIBRARY_ITEMS=3000`
3. Use a model with larger context (e.g., `google/gemini-pro-1.5` with 1M context)

#### 7. OpenRouter Rate Limit

**Error:**
```
openrouter_rate_limit
```

**Cause:** Too many requests or hitting free tier limits
**Fix:** Add credits to your OpenRouter account or wait for rate limit reset

### Debugging Commands

```bash
# Check what libraries exist
plex-rec recommend libraries

# Check user IDs
psql $DATABASE_URL -c "SELECT id, username FROM users;"

# Check watch stats for a user
psql $DATABASE_URL -c "
  SELECT ws.total_play_count, lc.title 
  FROM watch_stats ws 
  JOIN library_content lc ON ws.plex_rating_key = lc.plex_rating_key 
  WHERE ws.user_id = 44 
  ORDER BY ws.total_play_count DESC 
  LIMIT 10;
"

# Check saved recommendations
plex-rec recommend list --user 44
```

---

## File Reference

| File | Purpose |
|------|---------|
| `config.py` | Environment settings via Pydantic |
| `cli.py` | All CLI commands (Typer) |
| `api.py` | REST endpoints (FastAPI) |
| `db/__init__.py` | Database connection management + auto-init |
| `db/schema.py` | PostgreSQL table definitions |
| `db/migrations/` | Versioned schema migrations |
| `sync/tautulli.py` | Tautulli sync service |
| `sync/plex.py` | Plex library sync service |
| `sync/tmdb.py` | TMDB enrichment (keywords, ratings, languages) |
| `recommend/engine.py` | LLM clients (Ollama & OpenRouter) |
| `recommend/service.py` | Recommendation orchestration |
| `recommend/feedback.py` | User feedback collection & metrics |
| `embeddings/service.py` | LanceDB vector embeddings for RAG |
