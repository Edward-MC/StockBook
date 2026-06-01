"""Notion fetch + block→text + chunking.

Networking (`fetch_page_blocks`, `fetch_database_pages`) and parsing
(`blocks_to_text`, `chunk_text`) are split so the parsers are unit-testable
without hitting Notion (same convention as quotes.py).

A NotionSource is either a page (crawl its blocks) or a database (crawl each
row page's blocks). v1 reads top-level blocks only — no recursive child fetch
(keeps sync simple; deep nesting is a Phase-3 concern, spec §14).
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .. import config

# Block types whose rich_text we treat as prose.
_TEXT_BLOCK_TYPES = (
    "paragraph", "heading_1", "heading_2", "heading_3",
    "bulleted_list_item", "numbered_list_item", "quote",
    "to_do", "toggle", "callout",
)


def _rich_text_to_str(rich: List[dict]) -> str:
    """Concatenate a Notion rich_text array into plain text."""
    out = []
    for r in rich or []:
        # Prefer plain_text; fall back to nested text.content.
        t = r.get("plain_text")
        if t is None:
            t = (r.get("text") or {}).get("content", "")
        out.append(t or "")
    return "".join(out)


def blocks_to_text(blocks: List[dict]) -> str:
    """Convert a list of Notion blocks into newline-joined plain text.
    Non-text blocks (images, dividers, embeds) are skipped."""
    lines: List[str] = []
    for b in blocks or []:
        btype = b.get("type")
        if btype not in _TEXT_BLOCK_TYPES:
            continue
        rich = (b.get(btype) or {}).get("rich_text", [])
        line = _rich_text_to_str(rich).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def chunk_text(text: str, max_chars: Optional[int] = None) -> List[str]:
    """Split text into chunks no longer than max_chars, preferring paragraph
    boundaries. Blank input yields no chunks. No content is dropped."""
    if max_chars is None:
        max_chars = config.RAG_CHUNK_CHARS
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: List[str] = []
    buf = ""
    for para in text.split("\n"):
        candidate = (buf + "\n" + para) if buf else para
        if len(candidate) <= max_chars:
            buf = candidate
            continue
        if buf:
            chunks.append(buf)
            buf = ""
        # A single paragraph longer than the limit → hard-split it.
        while len(para) > max_chars:
            chunks.append(para[:max_chars])
            para = para[max_chars:]
        buf = para
    if buf:
        chunks.append(buf)
    return chunks


# --------------------------------------------------------------------------- #
# Networking (thin wrappers over notion-client; not unit-tested).
# --------------------------------------------------------------------------- #
def _client():
    from notion_client import Client  # local import so the dep is optional until used
    if not config.NOTION_TOKEN:
        raise RuntimeError("NOTION_TOKEN 未配置")
    return Client(auth=config.NOTION_TOKEN)


def notion_page_url(page_id: str) -> str:
    """Public-style URL for a page id (dashes stripped is fine for linking)."""
    return "https://www.notion.so/" + (page_id or "").replace("-", "")


def fetch_page_blocks(page_id: str) -> List[dict]:
    """All top-level blocks of a page (paginated)."""
    client = _client()
    blocks: List[dict] = []
    cursor = None
    while True:
        resp = client.blocks.children.list(block_id=page_id, start_cursor=cursor)
        blocks.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return blocks


def fetch_database_page_ids(database_id: str) -> List[str]:
    """Ids of all pages (rows) in a database (paginated)."""
    client = _client()
    ids: List[str] = []
    cursor = None
    while True:
        resp = client.databases.query(database_id=database_id, start_cursor=cursor)
        ids.extend(p["id"] for p in resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return ids


def page_title(page_id: str) -> str:
    """Best-effort page title for title_path labelling."""
    try:
        client = _client()
        page = client.pages.retrieve(page_id=page_id)
        props = page.get("properties", {})
        for prop in props.values():
            if prop.get("type") == "title":
                return _rich_text_to_str(prop.get("title", [])).strip() or "(无标题)"
    except Exception:
        pass
    return "(无标题)"


# How deep to follow nested child pages before stopping (guards against
# pathological trees / cycles). 5 levels is plenty for hand-organised notes.
_MAX_DEPTH = 5


def _child_refs(blocks: List[dict]) -> List[Dict[str, str]]:
    """Extract nested child pages/databases from a page's blocks. Notion puts
    a child page's title inline on the block, so we get it without an extra
    API call. Returns [{id, kind, title}]."""
    refs: List[Dict[str, str]] = []
    for b in blocks or []:
        bt = b.get("type")
        if bt in ("child_page", "child_database"):
            refs.append({
                "id": b.get("id", ""),
                "kind": "database" if bt == "child_database" else "page",
                "title": (b.get(bt) or {}).get("title", "") or "(无标题)",
            })
    return refs


def crawl_source(notion_id: str, kind: str) -> List[Dict[str, str]]:
    """Yield {page_id, url, title, text} for every page under a source,
    recursing into nested child pages/databases (spec §14 enhancement).

    `title` is a breadcrumb path ("父 / 子 / 孙") so citations locate the right
    sub-page. A database expands to its row pages; a page yields its own text
    plus everything beneath it. Pages with no prose of their own (pure
    containers of child pages) contribute nothing themselves but are still
    descended into.
    """
    out: List[Dict[str, str]] = []
    seen: set = set()  # guard against cycles / duplicate links

    def walk_page(pid: str, title_path: str, depth: int) -> None:
        if depth > _MAX_DEPTH or pid in seen:
            return
        seen.add(pid)
        blocks = fetch_page_blocks(pid)
        text = blocks_to_text(blocks)
        if text.strip():
            out.append({
                "page_id": pid,
                "url": notion_page_url(pid),
                "title": title_path,
                "text": text,
            })
        for ref in _child_refs(blocks):
            child_path = f"{title_path} / {ref['title']}"
            if ref["kind"] == "database":
                walk_database(ref["id"], child_path, depth + 1)
            else:
                walk_page(ref["id"], child_path, depth + 1)

    def walk_database(dbid: str, title_path: str, depth: int) -> None:
        if depth > _MAX_DEPTH or dbid in seen:
            return
        seen.add(dbid)
        for row_id in fetch_database_page_ids(dbid):
            walk_page(row_id, f"{title_path} / {page_title(row_id)}", depth + 1)

    if kind == "database":
        walk_database(notion_id, page_title(notion_id), 0)
    else:
        walk_page(notion_id, page_title(notion_id), 0)
    return out
