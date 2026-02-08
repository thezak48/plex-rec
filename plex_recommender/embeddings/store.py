"""LanceDB vector store for content embeddings."""

import time
from pathlib import Path
from typing import Any

import lancedb
import pyarrow as pa

from plex_recommender.config import get_settings
from plex_recommender.logging import get_logger
from plex_recommender.recommend.engine import OllamaClient

logger = get_logger(__name__)

# LanceDB table schema for library content embeddings
CONTENT_SCHEMA = pa.schema(
    [
        pa.field("plex_rating_key", pa.string()),
        pa.field("library_section_id", pa.int32()),
        pa.field("content_type", pa.string()),
        pa.field("title", pa.string()),
        pa.field("year", pa.int32()),
        pa.field("text_content", pa.string()),  # The text that was embedded
        pa.field("vector", pa.list_(pa.float32())),  # Embedding vector
        pa.field("updated_at", pa.float64()),  # Unix timestamp
    ]
)


class VectorStore:
    """LanceDB-based vector store for library content embeddings.

    Stores embeddings of library content (movies, shows) for fast similarity
    search during recommendation generation. This enables RAG-based recommendations
    where we only send the most relevant items to the LLM instead of the entire library.
    """

    TABLE_NAME = "library_embeddings"

    def __init__(self, db_path: str | None = None):
        """Initialize the vector store.

        Args:
            db_path: Path to LanceDB database directory. Defaults to configured path.
        """
        settings = get_settings()
        self.db_path = db_path or settings.lancedb_path
        self._db: lancedb.DBConnection | None = None
        self._table: lancedb.table.Table | None = None
        self._ollama: OllamaClient | None = None
        self._embedding_dim: int | None = None

    @property
    def db(self) -> lancedb.DBConnection:
        """Get or create database connection."""
        if self._db is None:
            # Ensure directory exists
            Path(self.db_path).mkdir(parents=True, exist_ok=True)
            self._db = lancedb.connect(self.db_path)
            logger.info("lancedb_connected", path=self.db_path)
        return self._db

    @property
    def ollama(self) -> OllamaClient:
        """Get or create Ollama client for embeddings."""
        if self._ollama is None:
            settings = get_settings()
            self._ollama = OllamaClient(model=settings.embeddings_model)
        return self._ollama

    def get_embedding_dim(self) -> int:
        """Get the embedding dimension from the model by generating a test embedding."""
        if self._embedding_dim is None:
            # Generate a test embedding to get dimension
            test_embedding = self.ollama.generate_embeddings("test")
            self._embedding_dim = len(test_embedding)
            logger.info("embedding_dim_detected", dim=self._embedding_dim)
        return self._embedding_dim

    def _get_schema(self) -> pa.Schema:
        """Get schema with correct embedding dimension."""
        dim = self.get_embedding_dim()
        return pa.schema(
            [
                pa.field("plex_rating_key", pa.string()),
                pa.field("library_section_id", pa.int32()),
                pa.field("content_type", pa.string()),
                pa.field("title", pa.string()),
                pa.field("year", pa.int32()),
                pa.field("text_content", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), dim)),  # Fixed-size list
                pa.field("updated_at", pa.float64()),
            ]
        )

    def _get_or_create_table(self) -> lancedb.table.Table:
        """Get or create the embeddings table."""
        if self._table is not None:
            return self._table

        try:
            self._table = self.db.open_table(self.TABLE_NAME)
            logger.info("opened_existing_table", table=self.TABLE_NAME)
        except Exception:
            # Table doesn't exist, create it
            schema = self._get_schema()
            self._table = self.db.create_table(
                self.TABLE_NAME,
                schema=schema,
                mode="create",
            )
            logger.info("created_table", table=self.TABLE_NAME)

        return self._table

    def content_to_text(self, content: dict[str, Any]) -> str:
        """Convert library content to embeddable text representation.

        Creates a rich text representation that captures the essence of the content
        for semantic similarity matching with user watch history.

        Args:
            content: Library content dict with metadata fields.

        Returns:
            Text string suitable for embedding generation (max ~1500 chars to fit
            within nomic-embed-text's 2048 token context window).
        """
        parts = []

        # Title and year
        title = content.get("title", "Unknown")
        year = content.get("year")
        if year:
            parts.append(f"{title} ({year})")
        else:
            parts.append(title)

        # Genres (important for similarity)
        genres = content.get("genres") or []
        if genres:
            parts.append(f"Genres: {', '.join(genres[:5])}")

        # Summary/description (rich semantic content) - reduced to 300 chars
        summary = content.get("summary") or ""
        if summary:
            summary = summary[:300] if len(summary) > 300 else summary
            parts.append(summary)

        # Keywords/tags (important for thematic similarity)
        keywords = content.get("keywords") or []
        if keywords:
            parts.append(f"Tags: {', '.join(keywords[:5])}")

        # Actors (useful for "more with this actor" type matches)
        actors = content.get("actors") or []
        if actors:
            parts.append(f"Cast: {', '.join(actors[:3])}")

        # Combine and enforce total length limit
        text = " | ".join(parts)
        if len(text) > 1500:
            text = text[:1500]
        return text

    def add_content(
        self,
        content_list: list[dict[str, Any]],
        batch_size: int = 100,
        progress_callback: Any | None = None,
    ) -> int:
        """Add or update library content embeddings.

        Args:
            content_list: List of library content dicts.
            batch_size: Number of embeddings to generate per batch.
            progress_callback: Optional callback(current, total) for progress.

        Returns:
            Number of items processed.
        """
        table = self._get_or_create_table()
        total = len(content_list)
        processed = 0

        logger.info("adding_content_embeddings", total=total, batch_size=batch_size)

        for i in range(0, total, batch_size):
            batch = content_list[i : i + batch_size]
            records = []

            for content in batch:
                text = self.content_to_text(content)
                try:
                    embedding = self.ollama.generate_embeddings(text)
                    records.append(
                        {
                            "plex_rating_key": str(content.get("plex_rating_key", "")),
                            "library_section_id": int(content.get("library_section_id", 0)),
                            "content_type": content.get("content_type", "movie"),
                            "title": content.get("title", ""),
                            "year": int(content.get("year") or 0),
                            "text_content": text,
                            "vector": embedding,
                            "updated_at": time.time(),
                        }
                    )
                except Exception as e:
                    logger.warning(
                        "embedding_generation_failed",
                        title=content.get("title"),
                        error=str(e),
                    )
                    continue

            if records:
                # Upsert by deleting existing and adding new
                keys = [r["plex_rating_key"] for r in records]
                key_filter = ", ".join(f"'{k}'" for k in keys)
                try:
                    table.delete(f"plex_rating_key IN ({key_filter})")
                except Exception:
                    pass  # Table might be empty
                table.add(records)

            processed += len(batch)
            if progress_callback:
                progress_callback(processed, total)

            logger.debug(
                "batch_processed",
                processed=processed,
                total=total,
                batch_records=len(records),
            )

            # Small delay between batches to prevent overwhelming Ollama
            if i + batch_size < total:
                time.sleep(0.1)

        logger.info("content_embeddings_complete", processed=processed)
        return processed

    def search_similar(
        self,
        query_text: str,
        limit: int = 200,
        library_section_id: int | None = None,
        exclude_keys: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Search for content similar to the query text.

        Args:
            query_text: Text to find similar content for.
            limit: Maximum number of results.
            library_section_id: Optional filter by library section.
            exclude_keys: Optional set of plex_rating_keys to exclude.

        Returns:
            List of similar content dicts with _distance field.
        """
        try:
            table = self._get_or_create_table()
        except Exception as e:
            logger.warning("table_not_found", error=str(e))
            return []

        # Generate query embedding
        query_embedding = self.ollama.generate_embeddings(query_text)

        # Build search query
        query = table.search(query_embedding).limit(limit * 2)  # Get extra for filtering

        # Apply library filter if specified
        if library_section_id is not None:
            query = query.where(f"library_section_id = {library_section_id}")

        results = query.to_list()

        # Filter out excluded keys and limit
        if exclude_keys:
            results = [r for r in results if r["plex_rating_key"] not in exclude_keys]

        return results[:limit]

    def search_by_watch_history(
        self,
        watched_content: list[dict[str, Any]],
        limit: int = 200,
        library_section_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Search for content similar to user's watch history.

        Aggregates embeddings from watched content to create a user preference
        vector, then searches for similar unwatched content.

        Args:
            watched_content: List of watched content with metadata.
            limit: Maximum number of results.
            library_section_id: Optional filter by library section.

        Returns:
            List of similar unwatched content.
        """
        if not watched_content:
            return []

        # Get watched keys to exclude
        watched_keys = {str(w.get("plex_rating_key", "")) for w in watched_content}

        # Weight more recent/completed watches higher
        weighted_texts = []
        for item in watched_content:
            text = self.content_to_text(item)
            # Higher weight for:
            # 1. Higher completion rate
            # 2. More recent watches
            # 3. Multiple plays
            completion = item.get("avg_completion") or item.get("percent_complete") or 50
            play_count = item.get("play_count", 1)

            # Simple weighting: completed shows matter more
            weight = 1 + (completion / 100) + (min(play_count, 3) * 0.2)
            weighted_texts.append((text, weight))

        # Sort by weight and take top items for query
        weighted_texts.sort(key=lambda x: x[1], reverse=True)
        top_texts = [t[0] for t in weighted_texts[:20]]

        # Create composite query text from top watched items
        composite_query = " | ".join(top_texts)

        # Search for similar content
        return self.search_similar(
            query_text=composite_query,
            limit=limit,
            library_section_id=library_section_id,
            exclude_keys=watched_keys,
        )

    def get_stats(self) -> dict[str, Any]:
        """Get statistics about the vector store.

        Returns:
            Dict with count, table info, etc.
        """
        try:
            table = self._get_or_create_table()
            count = table.count_rows()

            # Get sample of content types
            sample = (
                table.search().limit(100).select(["content_type", "library_section_id"]).to_list()
            )
            content_types = {}
            library_sections = {}
            for row in sample:
                ct = row.get("content_type", "unknown")
                content_types[ct] = content_types.get(ct, 0) + 1
                ls = row.get("library_section_id", 0)
                library_sections[ls] = library_sections.get(ls, 0) + 1

            return {
                "total_embeddings": count,
                "content_types": content_types,
                "library_sections": library_sections,
                "db_path": self.db_path,
                "embedding_dim": self._embedding_dim,
            }
        except Exception as e:
            return {
                "total_embeddings": 0,
                "error": str(e),
                "db_path": self.db_path,
            }

    def clear(self, library_section_id: int | None = None) -> int:
        """Clear embeddings from the store.

        Args:
            library_section_id: If specified, only clear this library's embeddings.

        Returns:
            Number of rows deleted.
        """
        try:
            table = self._get_or_create_table()
            if library_section_id is not None:
                count_before = table.count_rows()
                table.delete(f"library_section_id = {library_section_id}")
                count_after = table.count_rows()
                deleted = count_before - count_after
            else:
                deleted = table.count_rows()
                self.db.drop_table(self.TABLE_NAME)
                self._table = None
            logger.info("embeddings_cleared", deleted=deleted)
            return deleted
        except Exception as e:
            logger.warning("clear_failed", error=str(e))
            return 0

    def close(self):
        """Clean up resources."""
        if self._ollama:
            self._ollama.close()
            self._ollama = None
        # LanceDB doesn't need explicit close
        self._db = None
        self._table = None
