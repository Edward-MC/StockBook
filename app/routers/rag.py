"""RAG Q&A API (spec §10). Three guards on every paid path: master switch
(config.RAG_ENABLED), read-only 403, and a daily rate limit."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import config, schemas
from ..database import get_db
from ..models import NotionSource
from ..rag import ask, limiter, store

router = APIRouter(prefix="/api/rag", tags=["rag"])

# Shared in-process limiter (single-user, single-process).
_limiter = limiter.DailyLimiter(config.RAG_DAILY_LIMIT)

# Live sync progress (single-user, single-process). /sync updates it as it runs;
# /sync/progress is polled by the widget. `phase` is idle|crawl|embed|store|done|error.
_sync_progress = {"phase": "idle", "running": False, "pages": 0,
                  "embed_done": 0, "embed_total": 0,
                  "current": "", "chunk_count": 0, "error": None}


def _require_enabled():
    if not config.RAG_ENABLED:
        raise HTTPException(status_code=403, detail="问答功能未启用")
    if config.READONLY:
        raise HTTPException(status_code=403, detail="只读模式下问答不可用")


def _today() -> str:
    return dt.date.today().isoformat()


@router.get("/status")
def status(db: Session = Depends(get_db)):
    """Always available (no guard) so the frontend can decide whether to show
    the widget."""
    sources = db.query(NotionSource).all()
    return {
        "enabled": config.RAG_ENABLED and not config.READONLY,
        "model": config.RAG_MODEL,
        "remaining_today": _limiter.remaining(_today()),
        "daily_limit": config.RAG_DAILY_LIMIT,
        "chunk_count": store.chunk_count(db),
        "sources": [
            {"id": s.id, "notion_id": s.notion_id, "title": s.title,
             "kind": s.kind,
             "last_synced_at": s.last_synced_at.isoformat() if s.last_synced_at else None}
            for s in sources
        ],
    }


@router.post("/ask", dependencies=[Depends(_require_enabled)])
def ask_question(payload: schemas.AskRequest, db: Session = Depends(get_db)):
    if not _limiter.allow(_today()):
        raise HTTPException(status_code=429, detail="今日问答已达上限,请明天再试")
    try:
        return ask.answer(db, payload.question)
    except RuntimeError as e:  # missing key, etc.
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/sources", dependencies=[Depends(_require_enabled)])
def add_source(payload: schemas.NotionSourceCreate, db: Session = Depends(get_db)):
    if db.query(NotionSource).filter_by(notion_id=payload.notion_id).first():
        raise HTTPException(status_code=400, detail="该来源已存在")
    src = NotionSource(**payload.model_dump())
    db.add(src); db.commit(); db.refresh(src)
    return {"id": src.id, "notion_id": src.notion_id, "title": src.title, "kind": src.kind}


@router.delete("/sources/{source_id}", dependencies=[Depends(_require_enabled)])
def delete_source(source_id: int, db: Session = Depends(get_db)):
    src = db.get(NotionSource, source_id)
    if src is None:
        raise HTTPException(status_code=404, detail="来源不存在")
    db.delete(src); db.commit()
    return {"ok": True}


@router.get("/sync/progress")
def sync_progress():
    """Current sync progress, polled by the widget. Always available so the
    bar can render even after a page reload mid-sync."""
    return dict(_sync_progress)


@router.post("/sync", dependencies=[Depends(_require_enabled)])
def sync(db: Session = Depends(get_db)):
    """Re-sync all sources (delete-and-rebuild). Embedding is local (free);
    no LLM call here (spec §7.2). Updates _sync_progress as it runs so the
    widget can poll /sync/progress; runs in uvicorn's threadpool, so polling
    stays responsive while this blocks."""
    if _sync_progress["running"]:
        raise HTTPException(status_code=409, detail="同步正在进行中")

    sources = db.query(NotionSource).all()
    _sync_progress.update(phase="crawl", running=True, pages=0, embed_done=0,
                          embed_total=0, current="", error=None)
    results = []
    try:
        for src in sources:
            _sync_progress["current"] = src.title or src.notion_id

            def _on_progress(phase, **info):
                _sync_progress["phase"] = phase
                if phase == "crawl":
                    _sync_progress["pages"] = info.get("pages", 0)
                elif phase == "embed":
                    _sync_progress["embed_done"] = info.get("done", 0)
                    _sync_progress["embed_total"] = info.get("total", 0)

            try:
                n = store.sync_source(db, src, on_progress=_on_progress)
                db.commit()
                results.append({"source_id": src.id, "title": src.title, "chunks": n})
            except Exception as e:  # one source failing shouldn't abort the rest
                db.rollback()
                results.append({"source_id": src.id, "title": src.title, "error": str(e)})
        chunk_count = store.chunk_count(db)
        _sync_progress.update(phase="done", chunk_count=chunk_count, current="")
        return {"results": results, "chunk_count": chunk_count}
    finally:
        _sync_progress["running"] = False
