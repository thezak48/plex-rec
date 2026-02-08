"""Embeddings module for RAG-based recommendations using LanceDB."""

from plex_recommender.embeddings.service import EmbeddingsService
from plex_recommender.embeddings.store import VectorStore

__all__ = ["VectorStore", "EmbeddingsService"]
