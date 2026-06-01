"""Local zero-cost embeddings via fastembed (ONNX, Chinese BGE).

The model is loaded lazily on first use and cached at module level (first call
downloads the model to fastembed's cache dir). No torch, no API cost.
"""
from __future__ import annotations

from typing import List

from .. import config

_model = None


def _get_model():
    global _model
    if _model is None:
        from fastembed import TextEmbedding
        _model = TextEmbedding(model_name=config.RAG_EMBED_MODEL)
    return _model


def embed_texts(texts: List[str]) -> List[List[float]]:
    """Embed a batch of texts → list of float vectors (one per input)."""
    if not texts:
        return []
    model = _get_model()
    return [list(map(float, v)) for v in model.embed(texts)]


def embed_one(text: str) -> List[float]:
    """Embed a single query string."""
    vecs = embed_texts([text])
    return vecs[0] if vecs else []
