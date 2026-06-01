"""Prompt assembly + Claude call (spec §3 answer stage).

build_prompt is pure (unit-tested). answer() retrieves, assembles, and calls
the Anthropic SDK with prompt caching on the system block. Answer style:
summary first, then cited excerpts with Notion links (spec §1).
"""
from __future__ import annotations

from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from .. import config
from . import embed, snapshot, store

_SYSTEM = (
    "你是用户的个人投资笔记助手。基于提供的【笔记片段】和【当前持仓】回答问题。"
    "先用一两句话给出摘要观点,再在下面列出引用的原文片段及其 Notion 链接。"
    "只依据提供的材料作答;材料不足时明说,不要编造。用中文回答。"
)


def build_prompt(question: str, chunks: List[Dict], snapshot_text: str) -> str:
    """Assemble the user-turn prompt from retrieved chunks + holdings snapshot.
    Each excerpt is capped at RAG_EXCERPT_CHARS to bound tokens."""
    cap = config.RAG_EXCERPT_CHARS
    parts = ["【当前持仓】", snapshot_text or "（无持仓数据）", "", "【笔记片段】"]
    if not chunks:
        parts.append("（知识库中未找到相关内容。）")
    else:
        for i, c in enumerate(chunks, 1):
            excerpt = (c.get("text") or "")[:cap]
            path = c.get("title_path") or ""
            url = c.get("notion_url") or ""
            parts.append(f"[{i}] 《{path}》\n{excerpt}\n来源: {url}")
    parts += ["", "【问题】", question]
    return "\n".join(parts)


def answer(db: Session, question: str) -> Dict:
    """Retrieve, assemble, and call Claude. Returns {answer, citations}.
    Citations are the retrieved chunks (so the UI can render links even if the
    model omits some)."""
    query_vec = embed.embed_one(question)
    chunks = store.search(db, query_vec, k=config.RAG_TOP_K)
    snap = snapshot.holdings_snapshot(db)
    prompt = build_prompt(question, chunks, snap)

    from anthropic import Anthropic
    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY 未配置")
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=config.RAG_MODEL,
        max_tokens=1024,
        system=[{"type": "text", "text": _SYSTEM,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return {
        "answer": text,
        "citations": [
            {"title_path": c["title_path"], "notion_url": c["notion_url"],
             "score": round(c["score"], 3)}
            for c in chunks
        ],
    }
