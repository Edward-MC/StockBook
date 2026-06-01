"""Knowledge-chunk persistence + brute-force cosine retrieval (spec §5).

Embeddings are stored as JSON in a Text column and loaded into numpy at query
time. For a few thousand chunks this is sub-millisecond and needs no vector
extension. `cosine_top_k` is a pure function (unit-tested); `search` wires it
to the DB. To scale past ~tens of thousands of chunks, swap the body of
`search` for sqlite-vec — callers are unaffected.
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional, Tuple

import numpy as np
from sqlalchemy.orm import Session

from .. import config
from ..models import KnowledgeChunk, NotionSource


def cosine_top_k(query: List[float], rows: List[Tuple[int, List[float]]],
                 k: int) -> List[Tuple[int, float]]:
    """Rank (id, vector) rows by cosine similarity to `query`; return top-k as
    (id, score) descending. Zero vectors score 0 (no div-by-zero)."""
    if not rows or k <= 0:
        return []
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


def search(db: Session, query_vec: List[float], k: Optional[int] = None) -> List[Dict]:
    """Return the top-k chunks most similar to query_vec as dicts with text,
    notion_url, title_path, score. An empty query vector (e.g. embedding of an
    empty string) yields no results rather than k arbitrary zero-score hits."""
    if not query_vec:
        return []
    if k is None:
        k = config.RAG_TOP_K
    all_rows = db.query(KnowledgeChunk).all()
    if not all_rows:
        return []
    rows = [(c.id, json.loads(c.embedding)) for c in all_rows]
    ranked = cosine_top_k(query_vec, rows, k)
    by_id = {c.id: c for c in all_rows}
    out: List[Dict] = []
    for cid, score in ranked:
        c = by_id[cid]
        out.append({
            "id": c.id, "text": c.text, "notion_url": c.notion_url,
            "title_path": c.title_path, "score": score,
        })
    return out


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
    if total:
        _report("embed", done=0, total=total)
        vectors = embed.embed_texts([r["text"] for r in records])
        for r, v in zip(records, vectors):
            r["embedding"] = v
        _report("embed", done=total, total=total)
    _report("store")
    n = replace_source_chunks(db, source, records)
    source.last_synced_at = dt.datetime.now()
    return n
