"""Lightweight embedding service for game similarity search.

Optional dependency — requires sentence-transformers:
    pip install sentence-transformers
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from valveye.config import settings

if TYPE_CHECKING:
    import numpy as np

    from valveye.domain import GameProfile


def _lazy_import():
    """Lazy import to avoid hard dependency."""
    try:
        import numpy as np
        from sentence_transformers import SentenceTransformer
        return SentenceTransformer, np
    except ImportError:
        return None, None


class GameEmbeddingService:
    """Embedding-based game similarity using sentence-transformers."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        SentenceTransformer, np = _lazy_import()
        if SentenceTransformer is None:
            raise ImportError(
                "sentence-transformers is required for embeddings. "
                "Install with: pip install sentence-transformers"
            )
        self._np = np
        self._model = SentenceTransformer(model_name)
        self._cache: dict[int, np.ndarray] = {}

    def embed_game(self, profile: GameProfile) -> np.ndarray:
        """Create embedding from game profile text."""
        text = self._build_text(profile)
        embedding = self._model.encode(text, normalize_embeddings=True)
        self._cache[profile.app_id] = embedding
        return embedding

    def embed_query(self, query: str) -> np.ndarray:
        """Embed a text query."""
        return self._model.encode(query, normalize_embeddings=True)

    def get_or_compute(self, profile: GameProfile) -> np.ndarray:
        """Get cached embedding or compute a new one."""
        if profile.app_id in self._cache:
            return self._cache[profile.app_id]
        return self.embed_game(profile)

    def similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        """Compute cosine similarity between two embeddings."""
        return float(self._np.dot(a, b))

    def _build_text(self, profile: GameProfile) -> str:
        """Combine profile fields into embedding text."""
        parts = [
            profile.title,
            profile.description or "",
            " ".join(profile.tags[:10]),
            " ".join(profile.relevance_tags[:5]),
        ]
        return " ".join(p for p in parts if p)


def create_embedding_service() -> GameEmbeddingService | None:
    """Create embedding service if enabled and available."""
    if not settings.embeddings_enabled:
        return None
    try:
        return GameEmbeddingService()
    except ImportError:
        return None
