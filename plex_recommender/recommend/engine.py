"""Ollama LLM client for generating recommendations."""

import hashlib
import json
from typing import Any

import httpx
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential

from plex_recommender.config import get_settings
from plex_recommender.logging import get_logger

logger = get_logger(__name__)


class OllamaError(Exception):
    """Base exception for Ollama errors."""

    pass


class OllamaTimeoutError(OllamaError):
    """Raised when Ollama request times out."""

    pass


class OllamaServerError(OllamaError):
    """Raised when Ollama returns a 5xx error."""

    pass


def _should_retry_ollama(retry_state) -> bool:
    """Determine if an Ollama error should be retried."""
    exc = retry_state.outcome.exception()
    if exc is None:
        return False
    # Retry on timeout and server errors, but not on other errors
    return isinstance(exc, (OllamaTimeoutError, OllamaServerError, httpx.TimeoutException))


class RecommendationItem(BaseModel):
    """A single recommendation from the LLM."""

    rating_key: str = Field(description="Plex rating key of the recommended item")
    title: str = Field(description="Title of the recommended content")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence score 0-1")
    reasoning: str = Field(description="Why this is recommended for the user")
    matching_factors: list[str] = Field(
        default_factory=list,
        description="Factors that contributed to this recommendation",
    )


class RecommendationResponse(BaseModel):
    """Response from the LLM containing recommendations."""

    recommendations: list[RecommendationItem]
    model_notes: str = Field(
        default="",
        description="Any notes from the model about the recommendations",
    )


class OllamaClient:
    """Client for interacting with Ollama API."""

    def __init__(self, url: str | None = None, model: str | None = None):
        settings = get_settings()
        self.base_url = (url or settings.ollama_url).rstrip("/")
        self.model = model or settings.ollama_model
        self.timeout = settings.ollama_timeout_seconds
        self.num_ctx = settings.ollama_num_ctx
        self._client = httpx.Client(timeout=float(self.timeout))

    def list_models(self) -> list[str]:
        """List all available models in Ollama."""
        try:
            response = self._client.get(f"{self.base_url}/api/tags")
            response.raise_for_status()
            data = response.json()
            return [m["name"] for m in data.get("models", [])]
        except Exception as e:
            logger.error("list_models_failed", error=str(e))
            return []

    def is_model_available(self, model: str | None = None) -> bool:
        """Check if a specific model is available."""
        model = model or self.model
        models = self.list_models()
        # Check both exact match and base name (without tag)
        base_model = model.split(":")[0]
        return any(m == model or m.startswith(f"{base_model}:") or m == base_model for m in models)

    def pull_model(self, model: str | None = None, stream: bool = True) -> bool:
        """Pull/download a model from Ollama registry.

        Args:
            model: Model name to pull. Defaults to configured model.
            stream: If True, streams progress updates.

        Returns:
            True if successful, False otherwise.
        """
        model = model or self.model
        logger.info("pulling_model", model=model)

        try:
            # Use a longer timeout for model downloads
            with httpx.Client(timeout=600.0) as client:
                response = client.post(
                    f"{self.base_url}/api/pull",
                    json={"name": model, "stream": stream},
                    timeout=600.0,
                )
                response.raise_for_status()

                if stream:
                    # Process streaming response
                    for line in response.iter_lines():
                        if line:
                            try:
                                data = json.loads(line)
                                status = data.get("status", "")
                                if "completed" in data and "total" in data:
                                    pct = (data["completed"] / data["total"]) * 100
                                    logger.info(
                                        "pull_progress", status=status, percent=f"{pct:.1f}%"
                                    )
                                elif status:
                                    logger.info("pull_status", status=status)
                            except json.JSONDecodeError:
                                pass

            logger.info("model_pulled", model=model)
            return True

        except Exception as e:
            logger.error("pull_model_failed", model=model, error=str(e))
            return False

    def ensure_model(self, model: str | None = None) -> bool:
        """Ensure a model is available, pulling it if necessary.

        Args:
            model: Model name. Defaults to configured model.

        Returns:
            True if model is available (was already or successfully pulled).
        """
        model = model or self.model

        if self.is_model_available(model):
            logger.info("model_available", model=model)
            return True

        logger.info("model_not_found_pulling", model=model)
        return self.pull_model(model)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=2, max=30),
        retry=lambda retry_state: _should_retry_ollama(retry_state),
    )
    def chat(
        self,
        messages: list[dict[str, str]],
        format_json: bool = True,
    ) -> dict[str, Any]:
        """Send a chat request to Ollama with timeout handling."""
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "num_ctx": self.num_ctx,
            },
        }
        if format_json:
            payload["format"] = "json"

        try:
            response = self._client.post(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=float(self.timeout),  # Explicit timeout per request
            )
            response.raise_for_status()
            return response.json()
        except httpx.TimeoutException as e:
            logger.error("ollama_timeout", timeout=self.timeout, error=str(e))
            raise OllamaTimeoutError(f"Ollama request timed out after {self.timeout}s") from e
        except httpx.HTTPStatusError as e:
            if e.response.status_code >= 500:
                logger.error("ollama_server_error", status=e.response.status_code)
                raise OllamaServerError(f"Ollama server error: {e.response.status_code}") from e
            raise

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30))
    def generate_embeddings(self, text: str) -> list[float]:
        """Generate embeddings for text using Ollama."""
        response = self._client.post(
            f"{self.base_url}/api/embed",
            json={
                "model": self.model,
                "input": text,
            },
        )
        response.raise_for_status()
        data = response.json()
        return data.get("embeddings", [[]])[0]

    def check_health(self) -> bool:
        """Check if Ollama is available."""
        try:
            response = self._client.get(f"{self.base_url}/api/tags")
            return response.status_code == 200
        except Exception:
            return False

    def close(self):
        """Close the HTTP client."""
        self._client.close()


class OpenRouterError(Exception):
    """Base exception for OpenRouter errors."""

    pass


class OpenRouterTimeoutError(OpenRouterError):
    """Raised when OpenRouter request times out."""

    pass


class OpenRouterServerError(OpenRouterError):
    """Raised when OpenRouter returns a 5xx error."""

    pass


class OpenRouterRateLimitError(OpenRouterError):
    """Raised when OpenRouter rate limit is hit."""

    pass


def _should_retry_openrouter(retry_state) -> bool:
    """Determine if an OpenRouter error should be retried."""
    exc = retry_state.outcome.exception()
    if exc is None:
        return False
    return isinstance(
        exc,
        (
            OpenRouterTimeoutError,
            OpenRouterServerError,
            OpenRouterRateLimitError,
            httpx.TimeoutException,
        ),
    )


class OpenRouterClient:
    """Client for interacting with OpenRouter API (OpenAI-compatible)."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
    ):
        settings = get_settings()
        self.api_key = api_key or settings.openrouter_api_key
        self.model = model or settings.openrouter_model
        self.base_url = (base_url or settings.openrouter_base_url).rstrip("/")
        self.timeout = settings.ollama_timeout_seconds  # Reuse timeout setting
        self.context_window = settings.openrouter_context_window

        if not self.api_key:
            raise ValueError(
                "OpenRouter API key is required. Set OPENROUTER_API_KEY environment variable."
            )

        self._client = httpx.Client(
            timeout=float(self.timeout),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "HTTP-Referer": "https://github.com/plex-recommender",  # Optional
                "X-Title": "Plex Recommender",  # Optional
            },
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=2, max=30),
        retry=lambda retry_state: _should_retry_openrouter(retry_state),
    )
    def chat(
        self,
        messages: list[dict[str, str]],
        format_json: bool = True,
    ) -> dict[str, Any]:
        """Send a chat request to OpenRouter."""
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": 16000,  # Response token limit
        }
        if format_json:
            payload["response_format"] = {"type": "json_object"}

        try:
            response = self._client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                timeout=float(self.timeout),
            )

            # Handle rate limiting
            if response.status_code == 429:
                logger.warning("openrouter_rate_limit", detail="Rate limit hit. Retrying...")
                raise OpenRouterRateLimitError("OpenRouter rate limit exceeded")

            response.raise_for_status()
            data = response.json()

            # Log the raw response for debugging
            logger.debug("openrouter_raw_response", response_keys=list(data.keys()))

            # Check for OpenRouter error response
            if "error" in data:
                error_msg = data["error"].get("message", str(data["error"]))
                error_code = data["error"].get("code", "unknown")
                logger.error("openrouter_api_error", code=error_code, message=error_msg)
                raise OpenRouterError(f"OpenRouter API error ({error_code}): {error_msg}")

            # OpenRouter returns OpenAI-compatible response
            # Convert to match Ollama response format
            if "choices" not in data or not data["choices"]:
                logger.error("openrouter_invalid_response", data=data)
                raise OpenRouterError(f"OpenRouter returned invalid response: {data}")

            content = data["choices"][0]["message"]["content"]
            return {
                "message": {
                    "role": "assistant",
                    "content": content,
                },
                "model": data.get("model", self.model),
                "usage": data.get("usage", {}),
            }

        except httpx.TimeoutException as e:
            logger.error("openrouter_timeout", timeout=self.timeout, error=str(e))
            raise OpenRouterTimeoutError(
                f"OpenRouter request timed out after {self.timeout}s"
            ) from e
        except httpx.HTTPStatusError as e:
            if e.response.status_code >= 500:
                logger.error("openrouter_server_error", status=e.response.status_code)
                raise OpenRouterServerError(
                    f"OpenRouter server error: {e.response.status_code}"
                ) from e
            if e.response.status_code == 429:
                raise OpenRouterRateLimitError("OpenRouter rate limit exceeded") from e
            raise

    def check_health(self) -> bool:
        """Check if OpenRouter is available."""
        try:
            # OpenRouter doesn't have a simple health endpoint, so we check the models list
            response = self._client.get(f"{self.base_url}/models")
            return response.status_code == 200
        except Exception:
            return False

    def list_models(self) -> list[str]:
        """List available models from OpenRouter."""
        try:
            response = self._client.get(f"{self.base_url}/models")
            response.raise_for_status()
            data = response.json()
            return [m["id"] for m in data.get("data", [])]
        except Exception as e:
            logger.error("openrouter_list_models_failed", error=str(e))
            return []

    def close(self):
        """Close the HTTP client."""
        self._client.close()


# Type alias for LLM client (either Ollama or OpenRouter)
LLMClient = OllamaClient | OpenRouterClient


def get_llm_client() -> LLMClient:
    """Get the appropriate LLM client based on settings."""
    settings = get_settings()

    if settings.llm_provider == "openrouter":
        logger.info("using_openrouter", model=settings.openrouter_model)
        return OpenRouterClient()
    else:
        logger.info("using_ollama", model=settings.ollama_model)
        return OllamaClient()


class RecommendationEngine:
    """Engine for generating personalized recommendations using LLM."""

    SYSTEM_PROMPT_TEMPLATE = """You are a movie and TV show recommendation expert. Analyze the user's watch history and preferences, then recommend unwatched content they would enjoy.

RESPOND WITH VALID JSON ONLY. Use this exact structure:
{{
    "recommendations": [
        {{
            "rating_key": "COPY EXACTLY from the [brackets] in the available content list",
            "title": "The title of the content",
            "confidence": 0.85,
            "reasoning": "SPECIFIC reason - e.g. 'You watched 5 sci-fi shows with 90%+ completion. This is highly-rated sci-fi with similar themes.'",
            "matching_factors": ["sci-fi genre match", "high user completion rate for similar content"]
        }}
    ],
    "model_notes": "Brief observation about user's preferences"
}}

IMPORTANCE WEIGHTS (use these to prioritize matching factors):
- Genre matching: {weight_genre:.0%}
- Actor/Cast matching: {weight_actor:.0%}
- Keywords/Themes: {weight_keyword:.0%}
- Studio/Network: {weight_studio:.0%}
- Language: {weight_language:.0%}
- Release Year proximity: {weight_year:.0%}

CRITICAL RULES:
1. rating_key MUST be copied exactly from the [brackets] in the available content list
2. confidence should be 0.7-0.95 for good matches based on watch history patterns
3. reasoning MUST be specific - reference actual genres/titles from their watch history
4. matching_factors MUST be specific factors like "comedy genre", "anime style", "Korean drama fan"
5. DO NOT use placeholder text like "factor1" or "Explanation of why..."
6. Only recommend from the provided list
7. Weight your confidence score based on the importance weights above"""

    def __init__(self, client: LLMClient | None = None):
        self.client = client or get_llm_client()
        self.settings = get_settings()

    @property
    def SYSTEM_PROMPT(self) -> str:
        """Build system prompt with configured weights."""
        return self.SYSTEM_PROMPT_TEMPLATE.format(
            weight_genre=self.settings.weight_genre,
            weight_actor=self.settings.weight_actor,
            weight_keyword=self.settings.weight_keyword,
            weight_studio=self.settings.weight_studio,
            weight_language=self.settings.weight_language,
            weight_year=self.settings.weight_year,
        )

    def _build_user_prompt(
        self,
        user_preferences: dict,
        watched_content: list[dict],
        available_content: list[dict],
        max_recommendations: int,
        feedback_history: dict[str, list[dict]] | None = None,
    ) -> str:
        """Build the user prompt with watch history and available content."""
        # Format watched content summary (use all items - limiting is done in service layer)
        watched_summary = []
        for item in watched_content:
            genres = item.get("genres") or []
            watched_summary.append(
                f"- {item.get('title')} ({item.get('year', 'N/A')}) "
                f"[{', '.join(genres[:3])}] "
                f"- watched {item.get('play_count', 1)}x, "
                f"{item.get('avg_completion') or 0:.0f}% complete"
            )

        # Format genre preferences
        genre_prefs = []
        for genre, score in sorted(
            user_preferences.get("genres", {}).items(),
            key=lambda x: x[1],
            reverse=True,
        )[:10]:
            genre_prefs.append(f"- {genre}: {score:.2f} affinity")

        # Format available content with all metadata for weights
        available_summary = []
        compact = self.settings.compact_prompt

        for item in available_content:
            genres = item.get("genres") or []
            actors = item.get("actors") or []
            keywords = item.get("keywords") or []
            languages = item.get("languages") or []
            studio = item.get("studio") or ""
            year = item.get("year", "")
            rating = item.get("rating")
            tmdb_rating = item.get("tmdb_rating")

            if compact:
                # Compact format: [KEY] Title (Year) G:a,b C:x,y S:studio L:en K:tag1,tag2 R:8.5
                # ~40% fewer tokens than verbose format
                parts = [f"[{item.get('plex_rating_key')}] {item.get('title')}"]
                if year:
                    parts[0] += f" ({year})"
                if genres:
                    parts.append(f"G:{','.join(g[:3] for g in genres[:2])}")
                if actors:
                    # First name only for actors
                    short_actors = [a.split()[0] if " " in a else a[:8] for a in actors[:2]]
                    parts.append(f"C:{','.join(short_actors)}")
                if studio:
                    parts.append(f"S:{studio[:12]}")
                if languages:
                    # First 2 chars of language
                    parts.append(f"L:{languages[0][:2]}")
                if keywords:
                    parts.append(f"K:{','.join(k[:8] for k in keywords[:2])}")
                if tmdb_rating:
                    parts.append(f"R:{tmdb_rating}")
                elif rating:
                    parts.append(f"R:{rating}")
                available_summary.append(" ".join(parts))
            else:
                # Verbose format (original)
                parts = [f"[{item.get('plex_rating_key')}] {item.get('title')} ({year})"]
                if genres:
                    parts.append(f"Genres: {', '.join(genres[:3])}")
                if actors:
                    parts.append(f"Cast: {', '.join(actors[:3])}")
                if studio:
                    parts.append(f"Studio: {studio}")
                if languages:
                    parts.append(f"Lang: {', '.join(languages[:2])}")
                if keywords:
                    parts.append(f"Tags: {', '.join(keywords[:3])}")
                if tmdb_rating:
                    parts.append(f"TMDB: {tmdb_rating}/10")
                elif rating:
                    parts.append(f"Rating: {rating}")
                available_summary.append("- " + " | ".join(parts))

        # Get a sample item for the example
        sample_item = available_content[0] if available_content else {}
        sample_key = sample_item.get("plex_rating_key", "FROM_LIST_ABOVE")
        sample_title = sample_item.get("title", "Title from list")

        # Build format legend for compact mode
        if compact:
            format_legend = """Format: [KEY] Title (Year) G:genres C:cast S:studio L:lang K:keywords R:rating
(G=Genre, C=Cast, S=Studio, L=Language, K=Keywords, R=Rating)"""
        else:
            format_legend = (
                "Format: [RATING_KEY] Title (Year) | Genres | Cast | Studio | Lang | Tags | Rating"
            )

        # Build feedback section if available
        feedback_section = self._build_feedback_section(feedback_history)

        return f"""## User Watch History Analysis

### Recently Watched Content (showing patterns):
{chr(10).join(watched_summary) or "No watch history available"}

### Genre Preferences (by watch time):
{chr(10).join(genre_prefs) or "No genre preferences computed yet"}

### Additional User Preferences:
- Preferred content rating: {user_preferences.get("preferred_content_rating", "Any")}
- Average watch completion: {user_preferences.get("avg_completion", 0):.0f}%
- Most active viewing time: {user_preferences.get("peak_viewing_time", "Unknown")}
{feedback_section}
## Available Unwatched Content ({len(available_content)} items)
{format_legend}

{chr(10).join(available_summary)}

## CRITICAL INSTRUCTIONS
1. ONLY use rating_key values from the list above (the numbers in square brackets)
2. DO NOT invent rating keys - copy them exactly from the list
3. For example, if you want to recommend "{sample_title}", use rating_key "{sample_key}"
4. PRIORITIZE content similar to items the user LOVED - these are confirmed hits
5. AVOID content similar to items the user DISLIKED or SKIPPED

Recommend up to {max_recommendations} items. Respond with JSON:
{{
    "recommendations": [
        {{
            "rating_key": "{sample_key}",
            "title": "{sample_title}",
            "confidence": 0.85,
            "reasoning": "Explanation of why user would enjoy this",
            "matching_factors": ["factor1", "factor2"]
        }}
    ],
    "model_notes": "Optional observations"
}}

Generate recommendations using ONLY rating keys from the available content list above."""

    def _build_feedback_section(self, feedback_history: dict[str, list[dict]] | None) -> str:
        """Build the feedback section for the prompt."""
        if not feedback_history:
            return ""

        sections = []

        # Loved items - highest priority signal
        loved = feedback_history.get("loved", [])
        if loved:
            loved_items = []
            for item in loved[:10]:
                genres = ", ".join(item.get("genres", [])[:3]) or "Unknown"
                loved_items.append(f"  - {item['title']} ({item.get('year', 'N/A')}) [{genres}]")
            sections.append(
                "### Previous Recommendations User LOVED (rated 8+/10):\n" + "\n".join(loved_items)
            )

        # Liked items - positive signal
        liked = feedback_history.get("liked", [])
        if liked:
            liked_items = []
            for item in liked[:5]:
                genres = ", ".join(item.get("genres", [])[:3]) or "Unknown"
                liked_items.append(f"  - {item['title']} ({item.get('year', 'N/A')}) [{genres}]")
            sections.append(
                "### Previous Recommendations User LIKED (rated 6-8/10):\n" + "\n".join(liked_items)
            )

        # Disliked items - negative signal
        disliked = feedback_history.get("disliked", [])
        if disliked:
            disliked_items = []
            for item in disliked[:5]:
                genres = ", ".join(item.get("genres", [])[:3]) or "Unknown"
                disliked_items.append(f"  - {item['title']} ({item.get('year', 'N/A')}) [{genres}]")
            sections.append(
                "### Previous Recommendations User DISLIKED (avoid similar):\n"
                + "\n".join(disliked_items)
            )

        # Skipped items - weak negative signal
        skipped = feedback_history.get("skipped", [])
        if skipped:
            skipped_items = []
            for item in skipped[:5]:
                genres = ", ".join(item.get("genres", [])[:3]) or "Unknown"
                skipped_items.append(f"  - {item['title']} ({item.get('year', 'N/A')}) [{genres}]")
            sections.append(
                "### Previous Recommendations User IGNORED (30+ days, never watched):\n"
                + "\n".join(skipped_items)
            )

        if sections:
            return "\n\n" + "\n\n".join(sections) + "\n"
        return ""

    def _hash_prompt(self, prompt: str) -> str:
        """Generate a hash of the prompt for reproducibility tracking."""
        return hashlib.sha256(prompt.encode()).hexdigest()[:16]

    def generate_recommendations(
        self,
        user_id: int,
        user_preferences: dict,
        watched_content: list[dict],
        available_content: list[dict],
        max_recommendations: int | None = None,
        feedback_history: dict[str, list[dict]] | None = None,
    ) -> tuple[list[RecommendationItem], str]:
        """Generate recommendations for a user.

        Args:
            user_id: Internal user ID.
            user_preferences: Dict with genre affinities, avg completion, etc.
            watched_content: List of watched item dicts.
            available_content: List of unwatched item dicts to recommend from.
            max_recommendations: Max number of recommendations to generate.
            feedback_history: Optional dict with 'loved', 'liked', 'disliked', 'skipped' lists.

        Returns:
            Tuple of (recommendations list, prompt hash for tracking)
        """
        if max_recommendations is None:
            max_recommendations = self.settings.max_recommendations_per_user

        if not available_content:
            logger.warning("no_available_content", user_id=user_id)
            return [], ""

        user_prompt = self._build_user_prompt(
            user_preferences,
            watched_content,
            available_content,
            max_recommendations,
            feedback_history,
        )
        prompt_hash = self._hash_prompt(user_prompt)

        logger.info(
            "generating_recommendations",
            user_id=user_id,
            available_count=len(available_content),
            prompt_hash=prompt_hash,
        )

        try:
            import time
            start = time.perf_counter()
            response = self.client.chat(
                messages=[
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                format_json=True,
            )
            llm_elapsed = time.perf_counter() - start
            logger.info(
                "llm_call_timing",
                user_id=user_id,
                available_count=len(available_content),
                elapsed_seconds=f"{llm_elapsed:.2f}",
            )

            # Log how long the LLM request took (client.chat may block)
            # We attempt to measure duration if the client provides timings
            # via transport, otherwise measure elapsed time in the caller.

            content = response.get("message", {}).get("content", "{}")
            logger.info(
                "llm_raw_response",
                response_length=len(content),
                preview=content[:500] if content else "empty",
            )
            parsed = json.loads(content)

            # Handle common LLM typos/variations in key names
            if "recommedations" in parsed and "recommendations" not in parsed:
                parsed["recommendations"] = parsed.pop("recommedations")
            if "recomendations" in parsed and "recommendations" not in parsed:
                parsed["recommendations"] = parsed.pop("recomendations")

            # Ensure recommendations is a list of dicts, not strings
            if "recommendations" in parsed:
                recs = parsed["recommendations"]
                if isinstance(recs, list) and len(recs) > 0 and isinstance(recs[0], str):
                    # LLM returned strings instead of objects - skip this response
                    logger.warning(
                        "malformed_recommendations",
                        user_id=user_id,
                        detail="LLM returned strings instead of recommendation objects",
                    )
                    return [], prompt_hash

            # Ensure recommendations exists
            if "recommendations" not in parsed:
                # Try to find any list in the response that looks like recommendations
                for key, value in parsed.items():
                    if isinstance(value, list) and len(value) > 0:
                        if isinstance(value[0], dict) and (
                            "rating_key" in value[0] or "title" in value[0]
                        ):
                            parsed["recommendations"] = value
                            break
                else:
                    parsed["recommendations"] = []

            # Validate and parse recommendations - handle partial failures gracefully
            valid_recs = []
            for rec_data in parsed.get("recommendations", []):
                if not isinstance(rec_data, dict):
                    continue
                try:
                    rec = RecommendationItem(**rec_data)
                    valid_recs.append(rec)
                except Exception as e:
                    logger.debug("skipping_invalid_recommendation", error=str(e), data=rec_data)
                    continue

            logger.debug(
                "parsed_recommendations",
                count=len(valid_recs),
                sample_keys=[r.rating_key for r in valid_recs[:5]] if valid_recs else [],
            )

            # Filter to only include items that exist in available content
            valid_keys = {item.get("plex_rating_key") for item in available_content}
            logger.debug(
                "available_keys_sample",
                count=len(valid_keys),
                sample=[k for k in list(valid_keys)[:5]],
            )

            filtered_recs = [rec for rec in valid_recs if rec.rating_key in valid_keys]

            if len(valid_recs) > 0 and len(filtered_recs) == 0:
                logger.warning(
                    "all_recommendations_filtered_out",
                    user_id=user_id,
                    parsed_count=len(valid_recs),
                    sample_returned_keys=[r.rating_key for r in valid_recs[:5]],
                    sample_valid_keys=list(valid_keys)[:5],
                )

            logger.info(
                "recommendations_generated",
                user_id=user_id,
                count=len(filtered_recs),
                prompt_hash=prompt_hash,
            )

            return filtered_recs, prompt_hash

        except json.JSONDecodeError as e:
            logger.error("json_parse_error", error=str(e), user_id=user_id)
            return [], prompt_hash
        except (ValueError, TypeError, KeyError) as e:
            # Validation errors from malformed LLM responses - log and return empty
            logger.warning("llm_response_validation_failed", error=str(e), user_id=user_id)
            return [], prompt_hash
        except Exception as e:
            logger.error("recommendation_generation_failed", error=str(e), user_id=user_id)
            raise

    def close(self):
        """Clean up resources."""
        self.client.close()
