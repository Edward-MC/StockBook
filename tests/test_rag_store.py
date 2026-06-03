"""Tests for the knowledge store: cosine ranking (pure) and DB round-trip."""
import json

from app.rag import store


def test_cosine_top_k_ranks_by_similarity():
    query = [1.0, 0.0]
    rows = [
        (1, [1.0, 0.0]),
        (2, [0.0, 1.0]),
        (3, [0.9, 0.1]),
    ]
    ranked = store.cosine_top_k(query, rows, k=2)
    assert [rid for rid, _ in ranked] == [1, 3]
    assert ranked[0][1] >= ranked[1][1]


def test_cosine_top_k_handles_zero_row_vector():
    # A zero ROW vector must not raise (div-by-zero) and scores 0.
    ranked = store.cosine_top_k([1.0, 0.0], [(1, [0.0, 0.0])], k=1)
    assert ranked[0][0] == 1
    assert ranked[0][1] == 0.0


def test_cosine_top_k_zero_query_vector():
    # A zero QUERY vector returns the first k ids with score 0 (no div-by-zero).
    ranked = store.cosine_top_k([0.0, 0.0], [(1, [1.0, 0.0]), (2, [0.0, 1.0])], k=2)
    assert {rid for rid, _ in ranked} == {1, 2}
    assert all(score == 0.0 for _, score in ranked)


def test_cosine_top_k_empty_rows():
    assert store.cosine_top_k([1.0, 0.0], [], k=3) == []


def test_cosine_top_k_k_zero_returns_empty():
    assert store.cosine_top_k([1.0, 0.0], [(1, [1.0, 0.0])], k=0) == []


def test_replace_source_chunks_round_trip(client):
    from app import database
    from app.models import NotionSource
    db = database.SessionLocal()
    try:
        src = NotionSource(notion_id="nid-1", title="策略", kind="page")
        db.add(src)
        db.commit()
        db.refresh(src)

        store.replace_source_chunks(db, src, [
            {"page_id": "p1", "url": "u1", "title_path": "策略", "text": "红利逻辑", "embedding": [0.1, 0.2]},
            {"page_id": "p1", "url": "u1", "title_path": "策略", "text": "中证500", "embedding": [0.3, 0.4]},
        ])
        db.commit()

        from app.models import KnowledgeChunk
        rows = db.query(KnowledgeChunk).filter_by(source_id=src.id).all()
        assert len(rows) == 2
        assert json.loads(rows[0].embedding) == [0.1, 0.2]
        assert rows[0].seq == 0 and rows[1].seq == 1

        store.replace_source_chunks(db, src, [
            {"page_id": "p1", "url": "u1", "title_path": "策略", "text": "仅一条", "embedding": [0.5, 0.6]},
        ])
        db.commit()
        rows = db.query(KnowledgeChunk).filter_by(source_id=src.id).all()
        assert len(rows) == 1 and rows[0].text == "仅一条"
    finally:
        db.close()


def test_search_returns_best_chunk(client):
    from app import database
    from app.models import NotionSource
    db = database.SessionLocal()
    try:
        src = NotionSource(notion_id="nid-2", title="策略", kind="page")
        db.add(src); db.commit(); db.refresh(src)
        store.replace_source_chunks(db, src, [
            {"page_id": "p1", "url": "u1", "title_path": "A", "text": "红利", "embedding": [1.0, 0.0]},
            {"page_id": "p1", "url": "u1", "title_path": "B", "text": "成长", "embedding": [0.0, 1.0]},
        ])
        db.commit()
        hits = store.search(db, [0.9, 0.1], k=1)
        assert len(hits) == 1
        assert hits[0]["text"] == "红利"
        assert hits[0]["notion_url"] == "u1"
        assert hits[0]["title_path"] == "A"
        assert store.chunk_count(db) == 2
        # An empty query vector returns nothing, not arbitrary zero-score hits.
        assert store.search(db, [], k=3) == []
    finally:
        db.close()


def test_limiter_counts_and_caps():
    from app.rag import limiter
    lim = limiter.DailyLimiter(limit=2)
    # allow() only checks; record() consumes — so failed calls don't burn quota.
    assert lim.allow("2026-05-31") is True
    lim.record("2026-05-31")
    assert lim.allow("2026-05-31") is True
    lim.record("2026-05-31")
    assert lim.allow("2026-05-31") is False   # 2 recorded, at limit
    assert lim.allow("2026-06-01") is True     # new day
    lim.record("2026-06-01")
    assert lim.remaining("2026-06-01") == 1


def test_limiter_allow_without_record_does_not_consume():
    from app.rag import limiter
    lim = limiter.DailyLimiter(limit=1)
    # Checking allow() repeatedly without record() must not exhaust the quota
    # (a failed Claude call calls allow() but never record()).
    assert lim.allow("d") is True
    assert lim.allow("d") is True
    assert lim.remaining("d") == 1


def test_sync_source_embeds_and_stores(client, monkeypatch):
    from app import database
    from app.models import NotionSource, KnowledgeChunk
    from app.rag import store, notion, embed

    monkeypatch.setattr(notion, "crawl_source",
                        lambda nid, kind, on_progress=None: [
                            {"page_id": "p1", "url": "u1", "title": "策略",
                             "text": "红利逻辑\n高股息偏好"},
                        ])
    monkeypatch.setattr(embed, "embed_texts",
                        lambda texts: [[0.1, 0.2] for _ in texts])

    db = database.SessionLocal()
    try:
        src = NotionSource(notion_id="nid", title="策略", kind="page")
        db.add(src); db.commit(); db.refresh(src)
        phases = []
        n = store.sync_source(db, src, on_progress=lambda phase, **i: phases.append(phase))
        db.commit()
        assert n >= 1
        rows = db.query(KnowledgeChunk).filter_by(source_id=src.id).all()
        assert len(rows) == n
        assert src.last_synced_at is not None
        # Progress is reported through the run: crawl → embed → store.
        assert "embed" in phases and "store" in phases
    finally:
        db.close()


def test_empty_crawl_does_not_wipe_existing_chunks(client, monkeypatch):
    # A re-sync that crawls nothing (page emptied / transient failure) must
    # leave the previously-indexed chunks intact and report -1, not delete them.
    from app import database
    from app.models import NotionSource, KnowledgeChunk
    from app.rag import store, notion, embed

    db = database.SessionLocal()
    try:
        src = NotionSource(notion_id="nid", title="策略", kind="page")
        db.add(src); db.commit(); db.refresh(src)
        # First sync: real content.
        monkeypatch.setattr(notion, "crawl_source",
                            lambda nid, kind, on_progress=None: [
                                {"page_id": "p1", "url": "u1", "title": "策略", "text": "红利逻辑"}])
        monkeypatch.setattr(embed, "embed_texts", lambda texts: [[0.1, 0.2] for _ in texts])
        n1 = store.sync_source(db, src); db.commit()
        assert n1 >= 1
        before = db.query(KnowledgeChunk).filter_by(source_id=src.id).count()
        assert before >= 1
        # Second sync: crawl returns nothing.
        monkeypatch.setattr(notion, "crawl_source", lambda nid, kind, on_progress=None: [])
        n2 = store.sync_source(db, src); db.commit()
        assert n2 == -1   # signals "no content found"
        after = db.query(KnowledgeChunk).filter_by(source_id=src.id).count()
        assert after == before   # NOT wiped
    finally:
        db.close()


def test_search_delegates_to_get_retriever(monkeypatch):
    from app.rag import store

    class FakeRetriever:
        def search(self, db, query_vec, k=None):
            return [{"id": 42, "text": "fake", "notion_url": "",
                     "title_path": "", "score": 1.0}]
    monkeypatch.setattr(store, "get_retriever", lambda: FakeRetriever())
    out = store.search(db=None, query_vec=[0.1, 0.2])
    assert out == [{"id": 42, "text": "fake", "notion_url": "",
                    "title_path": "", "score": 1.0}]


def test_get_retriever_is_singleton(monkeypatch):
    from app.rag import store
    monkeypatch.setattr(store, "_retriever", None)
    r1 = store.get_retriever()
    r2 = store.get_retriever()
    assert r1 is r2
    assert isinstance(r1, store.NumpyCosineRetriever)
