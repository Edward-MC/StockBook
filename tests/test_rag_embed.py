"""Embedder interface: shim behavior + swappability without loading fastembed."""
from app.rag import embed


def test_embed_texts_empty_returns_empty_without_model():
    # empty list returns [] without calling _get_model
    assert embed.embed_texts([]) == []


def test_get_embedder_is_singleton(monkeypatch):
    monkeypatch.setattr(embed, "_embedder", None)   # start clean
    e1 = embed.get_embedder()
    e2 = embed.get_embedder()
    assert e1 is e2
    assert isinstance(e1, embed.FastembedEmbedder)


def test_module_shims_delegate_to_get_embedder(monkeypatch):
    class FakeEmbedder:
        def embed_texts(self, texts):
            return [[1.0, 2.0] for _ in texts]
        def embed_one(self, text):
            return [9.0]
    monkeypatch.setattr(embed, "get_embedder", lambda: FakeEmbedder())
    assert embed.embed_texts(["a", "b"]) == [[1.0, 2.0], [1.0, 2.0]]
    assert embed.embed_one("q") == [9.0]
