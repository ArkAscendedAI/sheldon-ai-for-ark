"""
Semantic cache for LLM responses.

Uses local sentence-transformer embeddings (all-MiniLM-L6-v2) for zero-cost
similarity matching. Caches complete agent responses so repeated/similar
questions are served instantly without LLM calls.

Architecture:
  1. Player asks a question
  2. Cache embeds the query (+ context like map name) → 384-dim vector
  3. Cosine similarity against all cached embeddings
  4. If similarity >= threshold → return cached response (HIT)
  5. If below threshold → full LLM call, then store result (MISS)
"""

import json
import logging
import pickle
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# Signals that a query's answer depends on map/server context
_CONTEXT_DEPENDENT_SIGNALS = frozenset([
    "where", "spawn", "location", "find", "map", "near", "closest",
    "server", "rate", "setting", "mod",
])


@dataclass
class CacheEntry:
    """A single cached query-response pair."""

    query: str
    response: str
    context_key: str
    created_at: float
    ttl: float
    hit_count: int = 0
    category: str = "general"


@dataclass
class CacheStats:
    """Running statistics for cache performance monitoring."""

    hits: int = 0
    misses: int = 0
    total_queries: int = 0
    total_similarity: float = 0.0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / max(total, 1)

    @property
    def avg_similarity(self) -> float:
        return self.total_similarity / max(self.total_queries, 1)

    def to_dict(self) -> dict:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "total_queries": self.total_queries,
            "hit_rate": round(self.hit_rate, 4),
            "avg_similarity": round(self.avg_similarity, 4),
        }


class SemanticCache:
    """
    Semantic similarity cache using local sentence-transformer embeddings.

    Zero API cost. ~5-10ms per lookup on CPU.
    """

    def __init__(
        self,
        model_name: str = "all-MiniLM-L6-v2",
        threshold: float = 0.90,
        default_ttl: float = 86400.0,  # 24 hours
        max_entries: int = 10_000,
        persist_path: Optional[str] = None,
    ):
        self.threshold = threshold
        self.default_ttl = default_ttl
        self.max_entries = max_entries
        self.persist_path = persist_path
        self.stats = CacheStats()

        # Lazy-load the embedding model (1-2s on first use)
        self._model = None
        self._model_name = model_name
        self._embedding_dim = 384  # all-MiniLM-L6-v2 dimension

        # Storage
        self._embeddings: np.ndarray = np.empty((0, self._embedding_dim), dtype=np.float32)
        self._entries: list[CacheEntry] = []

        # Load persisted cache if available
        if persist_path:
            self._load(persist_path)

    @property
    def _embed_model(self):
        """Lazy-load the sentence-transformer model on first use."""
        if self._model is None:
            logger.info(f"Loading embedding model: {self._model_name}")
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self._model_name)
            logger.info("Embedding model loaded")
        return self._model

    def _embed(self, text: str) -> np.ndarray:
        """Embed a text string into a normalized vector."""
        emb = self._embed_model.encode([text], normalize_embeddings=True)
        return emb[0].astype(np.float32)

    @staticmethod
    def _make_key(query: str, context: Optional[dict] = None) -> str:
        """
        Build a cache key from query + relevant context.

        Context-dependent queries (e.g., "where do rexes spawn") include
        map/server info. Context-independent queries use raw query only.
        """
        query_lower = query.lower()
        is_context_dependent = any(
            signal in query_lower for signal in _CONTEXT_DEPENDENT_SIGNALS
        )

        parts = [query]
        if is_context_dependent and context:
            for key in sorted(context.keys()):
                if context[key]:
                    parts.append(f"{key}: {context[key]}")

        return " | ".join(parts)

    def lookup(
        self, query: str, context: Optional[dict] = None
    ) -> tuple[Optional[str], float]:
        """
        Look up a semantically similar cached response.

        Returns:
            (cached_response, similarity_score) if a match is found above threshold.
            (None, best_similarity_score) if no match found.
        """
        self.stats.total_queries += 1

        if len(self._entries) == 0:
            self.stats.misses += 1
            return None, 0.0

        cache_key = self._make_key(query, context)
        query_emb = self._embed(cache_key)

        # Cosine similarity (embeddings are pre-normalized)
        similarities = self._embeddings @ query_emb
        best_idx = int(np.argmax(similarities))
        best_score = float(similarities[best_idx])
        self.stats.total_similarity += best_score

        entry = self._entries[best_idx]

        # Check expiry
        if time.time() - entry.created_at > entry.ttl:
            self._evict(best_idx)
            self.stats.misses += 1
            return None, best_score

        # Check threshold
        if best_score >= self.threshold:
            entry.hit_count += 1
            self.stats.hits += 1
            logger.debug(
                f"Cache HIT (score={best_score:.3f}): "
                f"'{query[:50]}' matched '{entry.query[:50]}'"
            )
            return entry.response, best_score

        self.stats.misses += 1
        logger.debug(
            f"Cache MISS (best_score={best_score:.3f}): '{query[:50]}'"
        )
        return None, best_score

    def store(
        self,
        query: str,
        response: str,
        context: Optional[dict] = None,
        ttl: Optional[float] = None,
        category: str = "general",
    ):
        """Store a query-response pair in the cache."""
        # Evict oldest if at capacity
        if len(self._entries) >= self.max_entries:
            self._evict_oldest()

        cache_key = self._make_key(query, context)
        emb = self._embed(cache_key)

        entry = CacheEntry(
            query=query,
            response=response,
            context_key=cache_key,
            created_at=time.time(),
            ttl=ttl or self.default_ttl,
            category=category,
        )

        self._embeddings = np.vstack([self._embeddings, emb.reshape(1, -1)])
        self._entries.append(entry)

        logger.debug(f"Cache STORE: '{query[:50]}' (category={category}, ttl={entry.ttl}s)")

    def purge_expired(self) -> int:
        """Remove all expired entries. Returns count of purged entries."""
        now = time.time()
        keep = [i for i, e in enumerate(self._entries) if now - e.created_at <= e.ttl]
        purged = len(self._entries) - len(keep)

        if purged > 0:
            self._embeddings = self._embeddings[keep] if keep else np.empty(
                (0, self._embedding_dim), dtype=np.float32
            )
            self._entries = [self._entries[i] for i in keep]
            logger.info(f"Purged {purged} expired cache entries")

        return purged

    def _evict(self, idx: int):
        """Remove a single entry by index."""
        self._embeddings = np.delete(self._embeddings, idx, axis=0)
        self._entries.pop(idx)

    def _evict_oldest(self):
        """Remove the oldest entry."""
        if not self._entries:
            return
        oldest_idx = min(range(len(self._entries)), key=lambda i: self._entries[i].created_at)
        self._evict(oldest_idx)

    @property
    def size(self) -> int:
        """Number of entries in the cache."""
        return len(self._entries)

    def get_stats(self) -> dict:
        """Get cache performance statistics."""
        return {
            **self.stats.to_dict(),
            "cache_size": self.size,
            "memory_mb": round(self._embeddings.nbytes / (1024 * 1024), 2),
        }

    def save(self, path: Optional[str] = None):
        """Persist cache to disk."""
        save_path = path or self.persist_path
        if not save_path:
            return

        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        with open(save_path, "wb") as f:
            pickle.dump(
                {
                    "embeddings": self._embeddings,
                    "entries": self._entries,
                    "stats": self.stats,
                },
                f,
            )
        logger.info(f"Cache saved to {save_path} ({self.size} entries)")

    def _load(self, path: str):
        """Load cache from disk."""
        cache_path = Path(path)
        if not cache_path.exists():
            logger.info(f"No cached data at {path}, starting fresh")
            return

        try:
            with open(cache_path, "rb") as f:
                data = pickle.load(f)
            self._embeddings = data["embeddings"]
            self._entries = data["entries"]
            self.stats = data.get("stats", CacheStats())
            logger.info(f"Cache loaded from {path} ({self.size} entries)")
        except Exception as e:
            logger.warning(f"Failed to load cache from {path}: {e}")
