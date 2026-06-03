"""Knowledge-chunk persistence + brute-force cosine retrieval (spec §5).

Embeddings are stored as JSON in a Text column and loaded into numpy at query
time. For a few thousand chunks this is sub-millisecond and needs no vector
extension. `cosine_top_k` is a pure function (unit-tested); `search` is a shim that
delegates to `get_retriever()`. Retriever is the swap point: to scale past
~tens of thousands of chunks, add a new Retriever (e.g. sqlite-vec) and
return it from `get_retriever()` — callers are unaffected.

`numpy` is imported lazily (inside the two retrieval helpers) so the core app
starts without it — the slim Docker image installs numpy only with the optional
RAG extras (requirements-rag.txt); the RAG router still imports this module at
startup but never touches numpy until a question is actually asked.
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional, Protocol, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import config
from ..models import KnowledgeChunk, NotionSource


def cosine_top_k(query: List[float], rows: List[Tuple[int, List[float]]],
                 k: int) -> List[Tuple[int, float]]:
    """Rank (id, vector) rows by cosine similarity to `query`; return top-k as
    (id, score) descending. Zero vectors score 0 (no div-by-zero)."""
    if not rows or k <= 0:
        return []
    import numpy as np
    q = np.asarray(query, dtype=np.float32)
    qn = np.linalg.norm(q)
    if qn == 0:
        return [(rid, 0.0) for rid, _ in rows[:k]]
    ids = [rid for rid, _ in rows]
    mat = np.asarray([vec for _, vec in rows], dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1)
    norms[norms == 0] = 1.0  # avoid div-by-zero; zero rows → score 0 below
    scores = (mat @ q) / (norms * qn)
    order = np.argsort(-scores)[:k]
    return [(ids[i], float(scores[i])) for i in order]


def replace_source_chunks(db: Session, source: NotionSource,
                          chunks: List[Dict]) -> int:
    """Delete this source's existing chunks, then insert the given ones (spec
    §4: resync = delete-and-rebuild). Each chunk dict needs page_id, url,
    title_path, text, embedding (list[float]). Returns count inserted.
    Caller commits."""
    db.query(KnowledgeChunk).filter(
        KnowledgeChunk.source_id == source.id
    ).delete(synchronize_session=False)
    for i, c in enumerate(chunks):
        db.add(KnowledgeChunk(
            source_id=source.id,
            notion_page_id=c["page_id"],
            notion_url=c.get("url", ""),
            title_path=c.get("title_path", ""),
            text=c["text"],
            embedding=json.dumps(c["embedding"]),
            seq=i,
        ))
    return len(chunks)


# Cached embedding matrix for retrieval. Keyed on a cheap (count, max_id)
# sentinel so it auto-invalidates whenever chunks are added/removed (resync,
# reset). Avoids re-loading and re-parsing every embedding on every question.
_embed_cache: Dict[str, object] = {"key": None, "ids": [], "matrix": None}


def _embedding_index(db: Session):
    """Return (ids, matrix) of all chunk embeddings, rebuilding the cache only
    when the chunk set changed. matrix is None when the KB is empty."""
    count = db.query(KnowledgeChunk.id).count()
    max_id = db.query(func.max(KnowledgeChunk.id)).scalar() or 0
    key = (count, max_id)
    if _embed_cache["key"] != key:
        import numpy as np
        rows = db.query(KnowledgeChunk.id, KnowledgeChunk.embedding).all()
        ids = [r[0] for r in rows]
        matrix = (np.asarray([json.loads(r[1]) for r in rows], dtype=np.float32)
                  if rows else None)
        _embed_cache.update(key=key, ids=ids, matrix=matrix)
    return _embed_cache["ids"], _embed_cache["matrix"]


class Retriever(Protocol):
    def search(self, db: Session, query_vec: List[float],
               k: Optional[int] = None) -> List[Dict]: ...


class NumpyCosineRetriever:
    """Default Retriever: brute-force cosine over the cached embedding matrix.
    Future sqlite-vec backend = a new Retriever, no call-site change."""
    def search(self, db: Session, query_vec: List[float],
               k: Optional[int] = None) -> List[Dict]:
        if not query_vec:
            return []
        if k is None:
            k = config.RAG_TOP_K
        ids, matrix = _embedding_index(db)
        if matrix is None:
            return []
        rows = list(zip(ids, matrix))  # (id, vector) — vector is a numpy row view
        ranked = cosine_top_k(query_vec, rows, k)
        if not ranked:
            return []
        top_ids = [cid for cid, _ in ranked]
        by_id = {c.id: c for c in
                 db.query(KnowledgeChunk).filter(KnowledgeChunk.id.in_(top_ids)).all()}
        out: List[Dict] = []
        for cid, score in ranked:
            c = by_id.get(cid)
            if c is None:
                continue
            out.append({
                "id": c.id, "text": c.text, "notion_url": c.notion_url,
                "title_path": c.title_path, "score": score,
            })
        return out


_retriever: Optional[Retriever] = None


def get_retriever() -> Retriever:
    """The configured retriever (singleton). Single change point for future
    backends (e.g. sqlite-vec) — no config knob yet (only one implementation)."""
    global _retriever
    if _retriever is None:
        _retriever = NumpyCosineRetriever()
    return _retriever


def search(db: Session, query_vec: List[float], k: Optional[int] = None) -> List[Dict]:
    """Shim → get_retriever().search (call sites unchanged)."""
    return get_retriever().search(db, query_vec, k)


def chunk_count(db: Session) -> int:
    return db.query(KnowledgeChunk).count()


def sync_source(db: Session, source: NotionSource, on_progress=None) -> int:
    """Crawl a Notion source, embed its pages' chunks, and replace stored
    chunks for it (delete-and-rebuild). Returns chunk count. Caller commits.

    `on_progress(phase, **info)` is called through the run with phase one of
    "crawl" (info: pages), "embed" (info: done, total), "store" — so callers
    can report sync progress."""
    import datetime as dt
    from . import embed, notion

    def _report(phase, **info):
        if on_progress:
            on_progress(phase, **info)

    pages = notion.crawl_source(
        source.notion_id, source.kind,
        on_progress=lambda pages: _report("crawl", pages=pages),
    )
    records = []
    for page in pages:
        for piece in notion.chunk_text(page["text"]):
            records.append({
                "page_id": page["page_id"], "url": page["url"],
                "title_path": page.get("title", ""), "text": piece,
            })
    total = len(records)
    # An empty crawl (page moved/emptied, or a transient Notion failure that
    # yields no pages) must NOT delete-and-rebuild — that would silently wipe a
    # previously-indexed source. Leave existing chunks intact and signal "no
    # content found" via a negative count so the caller can surface it.
    if total == 0:
        _report("store")
        source.last_synced_at = dt.datetime.now()
        return -1
    _report("embed", done=0, total=total)
    vectors = embed.embed_texts([r["text"] for r in records])
    for r, v in zip(records, vectors):
        r["embedding"] = v
    _report("embed", done=total, total=total)
    _report("store")
    n = replace_source_chunks(db, source, records)
    source.last_synced_at = dt.datetime.now()
    return n
