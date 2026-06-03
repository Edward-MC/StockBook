"""Local zero-cost embeddings via fastembed (ONNX, Chinese BGE).

Embedder is the swap point: one backend today (FastembedEmbedder), selected
via get_embedder(). The model loads lazily on first use (first call downloads
to fastembed's cache). No torch, no API cost. Module-level embed_texts/embed_one
stay as thin shims so existing call sites and tests are untouched.
"""
from __future__ import annotations

from typing import List, Optional, Protocol

from .. import config


class Embedder(Protocol):
    def embed_texts(self, texts: List[str]) -> List[List[float]]: ...
    def embed_one(self, text: str) -> List[float]: ...


class FastembedEmbedder:
    """Default Embedder: lazy fastembed model, cached on the instance."""
    def __init__(self) -> None:
        self._model = None

    def _get_model(self):
        if self._model is None:
            from fastembed import TextEmbedding
            self._model = TextEmbedding(model_name=config.RAG_EMBED_MODEL)
        return self._model

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        model = self._get_model()
        return [list(map(float, v)) for v in model.embed(texts)]

    def embed_one(self, text: str) -> List[float]:
        vecs = self.embed_texts([text])
        return vecs[0] if vecs else []


_embedder: Optional[Embedder] = None


def get_embedder() -> Embedder:
    """The configured embedder (singleton). Single change point for future
    backends — no config knob yet (only one implementation exists)."""
    global _embedder
    if _embedder is None:
        _embedder = FastembedEmbedder()
    return _embedder


def embed_texts(texts: List[str]) -> List[List[float]]:
    """Shim → get_embedder().embed_texts (call sites unchanged)."""
    return get_embedder().embed_texts(texts)


def embed_one(text: str) -> List[float]:
    """Shim → get_embedder().embed_one (call sites/tests unchanged)."""
    return get_embedder().embed_one(text)
