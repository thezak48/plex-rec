"""TMDB API client for fetching keywords and metadata enrichment."""

from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from plex_recommender.config import get_settings
from plex_recommender.db import get_db_cursor
from plex_recommender.logging import get_logger

logger = get_logger(__name__)

TMDB_API_BASE = "https://api.themoviedb.org/3"


class TMDBClient:
    """Client for The Movie Database (TMDB) API."""

    def __init__(self, api_token: str | None = None):
        settings = get_settings()
        self.api_token = api_token or settings.tmdb_api_token
        if not self.api_token:
            raise ValueError("TMDB_API_TOKEN is not configured")

        self._client = httpx.Client(
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {self.api_token}",
                "Accept": "application/json",
            },
        )

    def _make_request(self, endpoint: str, params: dict | None = None) -> dict[str, Any]:
        """Make a request to the TMDB API."""
        url = f"{TMDB_API_BASE}{endpoint}"
        response = self._client.get(url, params=params or {})
        response.raise_for_status()
        return response.json()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def search_movie(self, title: str, year: int | None = None) -> dict | None:
        """Search for a movie by title and optionally year."""
        params = {"query": title}
        if year:
            params["year"] = year

        data = self._make_request("/search/movie", params)
        results = data.get("results", [])

        if not results:
            return None

        # If year provided, try to find exact match
        if year:
            for result in results:
                release_date = result.get("release_date", "")
                if release_date and release_date.startswith(str(year)):
                    return result

        # Return first result
        return results[0]

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def search_tv(self, title: str, year: int | None = None) -> dict | None:
        """Search for a TV show by title and optionally year."""
        params = {"query": title}
        if year:
            params["first_air_date_year"] = year

        data = self._make_request("/search/tv", params)
        results = data.get("results", [])

        if not results:
            return None

        # If year provided, try to find exact match
        if year:
            for result in results:
                first_air = result.get("first_air_date", "")
                if first_air and first_air.startswith(str(year)):
                    return result

        return results[0]

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def get_movie_keywords(self, movie_id: int) -> list[str]:
        """Get keywords for a movie by TMDB ID."""
        data = self._make_request(f"/movie/{movie_id}/keywords")
        keywords = data.get("keywords", [])
        return [kw["name"] for kw in keywords]

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def get_tv_keywords(self, tv_id: int) -> list[str]:
        """Get keywords for a TV show by TMDB ID."""
        data = self._make_request(f"/tv/{tv_id}/keywords")
        keywords = data.get("results", [])
        return [kw["name"] for kw in keywords]

    def find_by_imdb_id(self, imdb_id: str) -> dict | None:
        """Find movie/TV by IMDB ID, including rating and language."""
        data = self._make_request(f"/find/{imdb_id}", {"external_source": "imdb_id"})
        movies = data.get("movie_results", [])
        if movies:
            return {
                "type": "movie",
                "id": movies[0]["id"],
                "vote_average": movies[0].get("vote_average"),
                "original_language": movies[0].get("original_language"),
            }
        tv = data.get("tv_results", [])
        if tv:
            return {
                "type": "tv",
                "id": tv[0]["id"],
                "vote_average": tv[0].get("vote_average"),
                "original_language": tv[0].get("original_language"),
            }
        return None

    def close(self):
        """Close the HTTP client."""
        self._client.close()


class TMDBEnrichmentService:
    """Service for enriching library content with TMDB data."""

    def __init__(self):
        settings = get_settings()
        if not settings.tmdb_api_token:
            raise ValueError("TMDB_API_TOKEN is required for enrichment")
        self.client = TMDBClient()

    def _extract_imdb_id(self, metadata_json: dict | None) -> str | None:
        """Extract IMDB ID from Plex metadata."""
        if not metadata_json:
            return None

        guids = metadata_json.get("guids", [])
        for guid in guids:
            if guid.startswith("imdb://"):
                return guid.replace("imdb://", "")
        return None

    def enrich_keywords(self, limit: int | None = None) -> int:
        """Enrich library_content with keywords, ratings, and language from TMDB.

        Args:
            limit: Max items to process (for testing). None = all.

        Returns:
            Number of items enriched.
        """
        logger.info("tmdb_enrichment_started", limit=limit)
        enriched = 0
        errors = 0

        # Get items without keywords OR without tmdb_rating OR without languages
        with get_db_cursor(commit=False) as cursor:
            query = """
                SELECT id, plex_rating_key, title, year, content_type, metadata_json, languages
                FROM library_content
                WHERE (keywords IS NULL OR array_length(keywords, 1) IS NULL)
                   OR tmdb_rating IS NULL
                   OR (languages IS NULL OR array_length(languages, 1) IS NULL)
                ORDER BY added_at DESC
            """
            if limit:
                query += f" LIMIT {limit}"
            cursor.execute(query)
            items = cursor.fetchall()

        logger.info("tmdb_items_to_enrich", count=len(items))

        for item in items:
            try:
                result = self._fetch_tmdb_data_for_item(item)
                if result:
                    with get_db_cursor() as cursor:
                        cursor.execute(
                            """UPDATE library_content
                               SET keywords = COALESCE(%s, keywords),
                                   tmdb_rating = COALESCE(%s, tmdb_rating),
                                   tmdb_id = COALESCE(%s, tmdb_id),
                                   languages = COALESCE(%s, languages)
                               WHERE id = %s""",
                            (
                                result.get("keywords"),
                                result.get("rating"),
                                result.get("tmdb_id"),
                                result.get("languages"),
                                item["id"],
                            ),
                        )
                    enriched += 1
                    if enriched % 50 == 0:
                        logger.info("tmdb_enrichment_progress", enriched=enriched)
            except Exception as e:
                errors += 1
                logger.debug(
                    "tmdb_enrichment_item_failed",
                    title=item["title"],
                    error=str(e),
                )
                # Rate limit handling - brief pause on error
                if "429" in str(e):
                    import time

                    time.sleep(1)

        logger.info("tmdb_enrichment_completed", enriched=enriched, errors=errors)
        return enriched

    def _fetch_tmdb_data_for_item(self, item: dict) -> dict | None:
        """Fetch keywords, rating, and language for a single library item.

        Returns:
            Dict with 'keywords', 'rating', 'tmdb_id', and 'languages', or None if not found.
        """
        title = item["title"]
        year = item["year"]
        content_type = item["content_type"]
        metadata = item.get("metadata_json") or {}

        tmdb_id = None
        tmdb_type = None
        rating = None
        original_language = None

        # Try to find by IMDB ID first (most accurate)
        imdb_id = self._extract_imdb_id(metadata)
        if imdb_id:
            result = self.client.find_by_imdb_id(imdb_id)
            if result:
                tmdb_id = result["id"]
                tmdb_type = result["type"]
                rating = result.get("vote_average")
                original_language = result.get("original_language")

        # Fall back to title search
        if not tmdb_id:
            if content_type == "movie":
                result = self.client.search_movie(title, year)
                if result:
                    tmdb_id = result["id"]
                    tmdb_type = "movie"
                    rating = result.get("vote_average")
                    original_language = result.get("original_language")
            else:
                result = self.client.search_tv(title, year)
                if result:
                    tmdb_id = result["id"]
                    tmdb_type = "tv"
                    rating = result.get("vote_average")
                    original_language = result.get("original_language")

        if not tmdb_id:
            return None

        # Fetch keywords
        keywords = None
        if tmdb_type == "movie":
            keywords = self.client.get_movie_keywords(tmdb_id)
        else:
            keywords = self.client.get_tv_keywords(tmdb_id)

        # Convert ISO 639-1 language code to full name
        languages = None
        if original_language:
            lang_name = self._iso_to_language_name(original_language)
            if lang_name:
                languages = [lang_name]

        return {
            "keywords": keywords if keywords else None,
            "rating": rating,
            "tmdb_id": tmdb_id,
            "languages": languages,
        }

    def _iso_to_language_name(self, iso_code: str) -> str | None:
        """Convert ISO 639-1 language code to full language name."""
        # Common language codes from TMDB
        language_map = {
            "en": "English",
            "es": "Spanish",
            "fr": "French",
            "de": "German",
            "it": "Italian",
            "pt": "Portuguese",
            "ru": "Russian",
            "ja": "Japanese",
            "ko": "Korean",
            "zh": "Chinese",
            "hi": "Hindi",
            "ar": "Arabic",
            "th": "Thai",
            "vi": "Vietnamese",
            "id": "Indonesian",
            "ms": "Malay",
            "tl": "Tagalog",
            "nl": "Dutch",
            "pl": "Polish",
            "sv": "Swedish",
            "da": "Danish",
            "no": "Norwegian",
            "fi": "Finnish",
            "tr": "Turkish",
            "el": "Greek",
            "he": "Hebrew",
            "cs": "Czech",
            "hu": "Hungarian",
            "ro": "Romanian",
            "uk": "Ukrainian",
            "bn": "Bengali",
            "ta": "Tamil",
            "te": "Telugu",
            "mr": "Marathi",
            "pa": "Punjabi",
            "cn": "Cantonese",
            "yue": "Cantonese",
            "cmn": "Mandarin",
        }
        return language_map.get(iso_code, iso_code.upper() if iso_code else None)

    def close(self):
        """Clean up resources."""
        self.client.close()
