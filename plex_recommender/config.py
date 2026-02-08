"""Configuration management using Pydantic Settings."""

from functools import lru_cache

from pydantic import Field, PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Plex configuration
    plex_url: str = Field(description="Plex server URL (e.g., http://localhost:32400)")
    plex_token: str = Field(description="Plex authentication token")

    # Tautulli configuration
    tautulli_url: str = Field(description="Tautulli API URL (e.g., http://localhost:8181)")
    tautulli_api_key: str = Field(description="Tautulli API key")

    # PostgreSQL configuration
    database_url: PostgresDsn = Field(
        default="postgresql://postgres:postgres@localhost:5432/plex_recommender",
        description="PostgreSQL connection string",
    )

    # Ollama configuration
    ollama_url: str = Field(
        default="http://localhost:11434",
        description="Ollama API URL",
    )
    ollama_model: str = Field(
        default="llama3.2",
        description="Ollama model to use for recommendations",
    )
    ollama_timeout_seconds: int = Field(
        default=120,
        description="Timeout in seconds for Ollama API calls",
    )
    ollama_num_ctx: int = Field(
        default=4096,
        description="Context window size for Ollama (increase for larger libraries)",
    )

    # LLM Provider configuration
    llm_provider: str = Field(
        default="ollama",
        description="LLM provider to use: 'ollama' or 'openrouter'",
    )

    # OpenRouter configuration (optional, for cloud LLM access)
    openrouter_api_key: str = Field(
        default="",
        description="OpenRouter API key (get from https://openrouter.ai/keys)",
    )
    openrouter_model: str = Field(
        default="anthropic/claude-3.5-sonnet",
        description="OpenRouter model to use (e.g., anthropic/claude-3.5-sonnet, openai/gpt-4-turbo)",
    )
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1",
        description="OpenRouter API base URL",
    )
    openrouter_context_window: int = Field(
        default=128000,
        description="Context window size for OpenRouter model (Claude=200k, GPT-4-turbo=128k)",
    )

    # Scheduling configuration
    sync_interval_minutes: int = Field(
        default=30,
        description="Interval between watch history syncs",
    )
    recommendation_day: str = Field(
        default="sunday",
        description="Day of week to generate recommendations",
    )
    recommendation_hour: int = Field(
        default=3,
        description="Hour to generate recommendations (24h format)",
    )

    # Recommendation settings
    min_confidence_score: float = Field(
        default=0.7,
        description="Minimum confidence score to apply recommendation label",
    )
    max_recommendations_per_user: int = Field(
        default=20,
        description="Maximum recommendations to generate per user",
    )
    label_prefix: str = Field(
        default="AI-Rec",
        description="Prefix for recommendation labels",
    )

    # Content limits for LLM prompts (0 = unlimited)
    max_library_items: int = Field(
        default=0,
        description="Max library items to send to LLM per batch (0 = all in one call)",
    )
    max_watch_history_items: int = Field(
        default=0,
        description="Max watch history items to send to LLM (0 = all)",
    )
    compact_prompt: bool = Field(
        default=True,
        description="Use compact format for prompts (reduces tokens by ~62%%)",
    )
    batch_processing: bool = Field(
        default=True,
        description="Process large libraries in batches to fit context window",
    )
    batch_size: int = Field(
        default=0,
        description="Items per batch (0 = auto-calculate based on context window)",
    )

    # RAG (Retrieval-Augmented Generation) settings
    use_rag: bool = Field(
        default=True,
        description="Use RAG to pre-filter relevant content before LLM (recommended for large libraries)",
    )
    lancedb_path: str = Field(
        default="./data/lancedb",
        description="Path to LanceDB database directory for vector storage",
    )
    embeddings_model: str = Field(
        default="nomic-embed-text",
        description="Ollama model to use for generating embeddings (e.g., nomic-embed-text, mxbai-embed-large)",
    )
    rag_top_k: int = Field(
        default=200,
        description="Number of most relevant items to retrieve via RAG for LLM processing",
    )

    def get_effective_context_window(self) -> int:
        """Get the context window size for the active LLM provider."""
        if self.llm_provider == "openrouter":
            return self.openrouter_context_window
        return self.ollama_num_ctx

    def get_effective_batch_size(self) -> int:
        """Calculate batch size based on context window if not explicitly set.

        Token budget breakdown (conservative estimates from real-world testing):
        - System prompt: ~600 tokens
        - Watch history: ~30 tokens/item × max_watch_history_items (or estimate 100)
        - Instructions + format legend: ~500 tokens
        - Response headroom: ~2000 tokens
        - Library items: ~60 tokens/item (compact) or ~120 tokens/item (verbose)
          (includes title, year, genres, actors, studio, language, keywords, rating)
        """
        if self.batch_size > 0:
            return self.batch_size

        # Calculate available tokens for library items
        system_tokens = 600
        watch_items = self.max_watch_history_items if self.max_watch_history_items > 0 else 100
        watch_tokens = watch_items * 30
        instruction_tokens = 500
        response_headroom = 2000

        overhead = system_tokens + watch_tokens + instruction_tokens + response_headroom
        context_window = self.get_effective_context_window()
        available = context_window - overhead

        # Tokens per library item (based on real-world measurement: ~59 tokens/item compact)
        tokens_per_item = 60 if self.compact_prompt else 120

        # Calculate batch size with 15% safety margin
        calculated = int((available / tokens_per_item) * 0.85)

        # Clamp to reasonable bounds (higher max for larger context windows)
        max_batch = 2000 if self.llm_provider == "openrouter" else 800
        return max(50, min(calculated, max_batch))

    # TMDB configuration (optional - for enriching keywords)
    tmdb_api_token: str | None = Field(
        default=None,
        description="TMDB API Read Access Token for keyword enrichment",
    )

    # Recommendation weights (should sum to 1.0)
    weight_genre: float = Field(
        default=0.25,
        description="Weight for genre matching (0.0-1.0)",
    )
    weight_actor: float = Field(
        default=0.20,
        description="Weight for actor/cast matching (0.0-1.0)",
    )
    weight_keyword: float = Field(
        default=0.25,
        description="Weight for keyword/theme matching (0.0-1.0)",
    )
    weight_studio: float = Field(
        default=0.15,
        description="Weight for studio/network matching (0.0-1.0)",
    )
    weight_language: float = Field(
        default=0.10,
        description="Weight for language matching (0.0-1.0)",
    )
    weight_year: float = Field(
        default=0.05,
        description="Weight for release year proximity (0.0-1.0)",
    )


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
