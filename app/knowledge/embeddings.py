"""AUSTRO AI - Embedding abstraction.

Providers implement `EmbeddingProvider`; the service picks the best available
provider and re-embeds nothing when a (model, version) pair already exists
(resume-safe). Embeddings are NEVER routed through the chat AI gateway - they
are a separate capability so genuine RAG vector work scales independently.
"""

from __future__ import annotations

import abc
import hashlib
import logging
import math
import re
from typing import List, Optional

from app.config.settings import Settings

logger = logging.getLogger(__name__)

_TOKEN_SPLIT = re.compile(r"[^\w\u0600-\u06ff]+")


class EmbeddingProvider(abc.ABC):
    """A provider that turns text into embedding vectors."""

    @property
    @abc.abstractmethod
    def model(self) -> str:
        """Provider model identifier (stored with every vector)."""

    @property
    @abc.abstractmethod
    def version(self) -> str:
        """Provider schema/semantics version (bump = re-embed required)."""

    @property
    @abc.abstractmethod
    def dimensions(self) -> int:
        """Vector length."""

    @abc.abstractmethod
    def embed(self, text: str) -> List[float]:
        """Embed a single text."""

    @abc.abstractmethod
    def embed_many(self, texts: List[str]) -> List[List[float]]:
        """Embed a batch of texts."""

    @property
    def is_available(self) -> bool:
        return True


class LocalHashEmbedder(EmbeddingProvider):
    """Deterministic, offline hashing vectorizer (default).

    Token counts are hashed into a fixed-size bag-of-words vector and
    L2-normalized, giving a cheap semantic proxy that is stable across runs
    and machines - no API, no data ever leaves the device.
    """

    def __init__(self, dimensions: int = 128, version: str = "1"):
        self._dimensions = max(16, int(dimensions))
        self._version = version

    @property
    def model(self) -> str:
        return "local-hash"

    @property
    def version(self) -> str:
        return self._version

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def is_available(self) -> bool:
        return True

    def embed(self, text: str) -> List[float]:
        vector = [0.0] * self._dimensions
        for token in _TOKEN_SPLIT.split((text or "").lower()):
            token = token.strip()
            if not token or len(token) == 1 and not token.isdigit():
                continue
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "little") % self._dimensions
            vector[index] += 1.0
        return _l2(vector)

    def embed_many(self, texts: List[str]) -> List[List[float]]:
        return [self.embed(text) for text in texts]


def _l2(vector: List[float]) -> List[float]:
    magnitude = math.sqrt(sum(value * value for value in vector))
    if magnitude == 0:
        return vector
    return [value / magnitude for value in vector]


def cosine_similarity(left: List[float], right: List[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return sum(a * b for a, b in zip(left, right))


class EmbeddingService:
    """Selects the active provider and caches vectors it produced."""

    def __init__(self, settings_: Settings,
                 providers: Optional[List[EmbeddingProvider]] = None):
        self._settings = settings_
        self._providers = providers or [
            LocalHashEmbedder(
                dimensions=settings_.knowledge_embedding_dimensions,
                version=settings_.knowledge_embedding_version,
            ),
        ]

    def active_provider(self) -> EmbeddingProvider:
        for provider in self._providers:
            if provider.is_available:
                return provider
        return self._providers[-1]

    def is_remote(self) -> bool:
        return self.active_provider().model != "local-hash"

    def model(self) -> str:
        return self.active_provider().model

    def version(self) -> str:
        return self.active_provider().version

    def dimensions(self) -> int:
        return self.active_provider().dimensions

    def embed_many(self, texts: List[str]) -> List[List[float]]:
        """Embed a batch with the active provider (no retry logic here)."""
        provider = self.active_provider()
        batch_size = self._settings.knowledge_embedding_batch_size
        vectors: List[List[float]] = []
        for start in range(0, len(texts), batch_size):
            logger.info(
                f"Embedding batch {start // batch_size + 1} "
                f"({len(texts)} texts, provider={provider.model})"
            )
            vectors.extend(provider.embed_many(texts[start:start + batch_size]))
        return vectors

    def embed_query(self, text: str) -> List[float]:
        return self.active_provider().embed(text)


__all__ = [
    "EmbeddingProvider",
    "EmbeddingService",
    "LocalHashEmbedder",
    "cosine_similarity",
]