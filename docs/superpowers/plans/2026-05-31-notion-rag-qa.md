# Notion RAG Q&A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a floating Q&A widget that answers questions from the user's Notion knowledge base (summary + cited excerpts), augmented with a compact live-holdings snapshot, via Claude API.

**Architecture:** A new `app/rag/` subpackage with four focused modules (Notion fetch/parse/chunk, local embedding, SQLite vector store + numpy cosine retrieval, prompt assembly + Claude call). Two new ORM tables (`NotionSource`, `KnowledgeChunk`) added in parallel to existing tables — no existing table is touched. A new `app/routers/rag.py` router exposes `/api/rag/*`, guarded by a master switch, a daily rate limit, and a read-only 403. Frontend adds a self-contained floating chat bubble. The design mirrors `quotes.py`'s "networking and parsing split for testability" convention.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, Pydantic v2, SQLite (single file), `fastembed` (ONNX, local zero-cost embeddings, Chinese BGE), `anthropic` (Claude SDK, default Haiku), `numpy` (brute-force cosine), `notion-client`. Python 3.9 (use `Optional`/`List`/`Dict`, not `X | None`). pytest + FastAPI TestClient.

**Spec:** `docs/superpowers/specs/2026-05-31-stockbook-rag-qa-design.md`
**Codebase reference:** `docs/architecture.md`

---

## Conventions (read before starting)

- **Python 3.9**: no `str | None` unions; use `typing.Optional`, `List`, `Dict`, `Tuple`.
- **Testing isolation** (architecture.md §8): tests use a per-test temp SQLite via the `client` fixture in `tests/conftest.py`. NEVER touch the real `stockbook.db`. For pure-function tests (parsing, cosine), no DB needed.
- **Network/parse split** (like `quotes.py`): every function that hits Notion or Claude must have its pure logic (parsing, chunking, prompt assembly, cosine) split into a network-free function that is unit-tested without mocking HTTP.
- **Migrations** (architecture.md decision 9): new tables are created by `Base.metadata.create_all` in `seed.create_schema()`. No Alembic. `KnowledgeChunk`/`NotionSource` are brand-new tables, so `create_all` handles them; no `ALTER TABLE` needed.
- **Commits**: this project uses git (see `.gitignore` and changelog). If `git status` errors with "not a repository", run `git init` once before the first commit. End commit messages with the project's standard trailer if one exists; otherwise a plain message is fine.
- **Run tests from project root**: `cd /Users/chenmeng/PycharmProjects/StockBook && .venv/bin/python -m pytest ...`

---

## File Structure

**Create:**
- `app/rag/__init__.py` — empty package marker.
- `app/rag/notion.py` — Notion fetch + block→text parse + chunking. Network (`fetch_*`) and parse (`blocks_to_text`, `chunk_text`) split.
- `app/rag/embed.py` — `fastembed` wrapper: lazy-loaded model, `embed_texts(list[str]) -> list[list[float]]`, `embed_one(str)`.
- `app/rag/store.py` — persist/replace `KnowledgeChunk` rows; `cosine_top_k` pure function; `search(db, query_vec, k)`.
- `app/rag/ask.py` — `build_prompt(question, chunks, snapshot)` (pure) + `answer(db, question)` (calls Claude).
- `app/rag/snapshot.py` — `holdings_snapshot(db) -> str` compact text from the dashboard payload.
- `app/routers/rag.py` — `/api/rag/status`, `/api/rag/sync`, `/api/rag/ask`, source management; guards.
- `app/rag/limiter.py` — in-process daily call counter.
- `tests/test_rag_notion.py`, `tests/test_rag_store.py`, `tests/test_rag_ask.py`, `tests/test_rag_api.py`.
- `templates/_rag_widget.html` — floating widget markup (included by `index.html`).
- `static/js/rag.js` — widget behaviour.
- `static/css/rag.css` — widget styles (or append to `style.css`; see Task 12).

**Modify:**
- `app/config.py` — new env vars.
- `app/models.py` — `NotionSource`, `KnowledgeChunk`.
- `app/schemas.py` — RAG request/response schemas.
- `main.py` — register the `rag` router.
- `requirements.txt` — new deps.
- `templates/index.html` — include the widget (hidden when read-only).
- `docs/architecture.md` — decisions + API + changelog.

---

## Task 1: Add dependencies

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Append the new dependencies**

Add these lines to `requirements.txt` (keep the existing pinned style; versions below are known-compatible floors — pin to whatever installs cleanly):

```
anthropic==0.39.0
fastembed==0.4.2
notion-client==2.2.1
numpy==1.26.4
```

- [ ] **Step 2: Install**

Run: `cd /Users/chenmeng/PycharmProjects/StockBook && .venv/bin/python -m pip install -r requirements.txt`
Expected: all four install. `fastembed` pulls `onnxruntime` (tens of MB, no torch). First model use downloads the BGE model to a cache dir (deferred until Task 6 runtime, not now).

- [ ] **Step 3: Verify imports**

Run: `cd /Users/chenmeng/PycharmProjects/StockBook && .venv/bin/python -c "import anthropic, fastembed, notion_client, numpy; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 4: Commit**

```bash
git add requirements.txt
git commit -m "chore: add RAG dependencies (anthropic, fastembed, notion-client, numpy)"
```

---

## Task 2: Configuration

**Files:**
- Modify: `app/config.py`

- [ ] **Step 1: Append RAG config to `app/config.py`**

Add at the end of the file (after `STATIC_DIR`):

```python
# --------------------------------------------------------------------------- #
# RAG Q&A (Phase 2). Feature is OFF unless explicitly enabled. Keys come only
# from the environment — never stored in the DB or sent to the frontend.
# --------------------------------------------------------------------------- #
RAG_ENABLED = os.getenv("STOCKBOOK_RAG_ENABLED", "").lower() in {"1", "true", "yes"}

NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Daily cap on /api/rag/ask calls (cost protection). Editable via env.
RAG_DAILY_LIMIT = int(os.getenv("STOCKBOOK_RAG_DAILY_LIMIT", "50"))

# Answer model — default to the cheap/fast Haiku; switchable via env.
RAG_MODEL = os.getenv("STOCKBOOK_RAG_MODEL", "claude-haiku-4-5-20251001")

# Retrieval / context trimming (cost protection).
RAG_TOP_K = int(os.getenv("STOCKBOOK_RAG_TOP_K", "5"))
RAG_CHUNK_CHARS = int(os.getenv("STOCKBOOK_RAG_CHUNK_CHARS", "1200"))      # chunk size when splitting
RAG_EXCERPT_CHARS = int(os.getenv("STOCKBOOK_RAG_EXCERPT_CHARS", "800"))    # per-chunk cap in prompt
RAG_EMBED_MODEL = os.getenv("STOCKBOOK_RAG_EMBED_MODEL", "BAAI/bge-small-zh-v1.5")
```

- [ ] **Step 2: Verify it imports**

Run: `cd /Users/chenmeng/PycharmProjects/StockBook && .venv/bin/python -c "from app import config; print(config.RAG_ENABLED, config.RAG_DAILY_LIMIT, config.RAG_MODEL)"`
Expected: `False 50 claude-haiku-4-5-20251001`

- [ ] **Step 3: Commit**

```bash
git add app/config.py
git commit -m "feat(rag): add RAG config (master switch, keys, daily limit, model)"
```

---

## Task 3: Data models

**Files:**
- Modify: `app/models.py`
- Test: `tests/test_rag_store.py` (created later; schema verified here via create_all)

- [ ] **Step 1: Append the two models to `app/models.py`**

Add after the `CashFlow` class. Note `embedding` is stored as a JSON-encoded list in a `Text` column (portable, no extension needed; decoded to numpy at query time):

```python
class NotionSource(Base):
    """A Notion page/database the user authorized for the knowledge base.
    Only these are crawled (spec §2:指定几个页面/库, not whole workspace)."""
    __tablename__ = "notion_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    notion_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String, nullable=False, default="")
    kind: Mapped[str] = mapped_column(String, nullable=False, default="page")  # "page" | "database"
    last_synced_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)

    chunks: Mapped[List["KnowledgeChunk"]] = relationship(
        back_populates="source", cascade="all, delete-orphan",
    )


class KnowledgeChunk(Base):
    """One embedded text fragment from a Notion page. Embedding is a JSON list
    in a Text column — portable across SQLite builds (no vector extension).
    Retrieval is brute-force numpy cosine over all chunks (spec §5)."""
    __tablename__ = "knowledge_chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("notion_sources.id"), nullable=False)
    notion_page_id: Mapped[str] = mapped_column(String, nullable=False)
    notion_url: Mapped[str] = mapped_column(String, nullable=False, default="")
    title_path: Mapped[str] = mapped_column(String, nullable=False, default="")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[str] = mapped_column(Text, nullable=False)  # JSON-encoded list[float]
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    source: Mapped["NotionSource"] = relationship(back_populates="chunks")
```

- [ ] **Step 2: Add the `Text` import**

In `app/models.py`, change the SQLAlchemy import line to include `Text`:

```python
from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, func
```

- [ ] **Step 3: Verify tables are created**

Run: `cd /Users/chenmeng/PycharmProjects/StockBook && STOCKBOOK_DATABASE_URL=sqlite:////tmp/rag_models_test.db .venv/bin/python -c "
from app import database, seed
seed.create_schema()
from sqlalchemy import inspect
names = set(inspect(database.engine).get_table_names())
assert 'notion_sources' in names and 'knowledge_chunks' in names, names
print('ok', sorted(names))
"`
Expected: prints `ok` and a list including `notion_sources`, `knowledge_chunks`. (Existing tables also present — confirms nothing broke.)

- [ ] **Step 4: Clean up the temp DB**

Run: `rm -f /tmp/rag_models_test.db`

- [ ] **Step 5: Commit**

```bash
git add app/models.py
git commit -m "feat(rag): add NotionSource and KnowledgeChunk models"
```

---

## Task 4: Notion parsing & chunking (pure, network-free)

**Files:**
- Create: `app/rag/__init__.py`, `app/rag/notion.py`
- Test: `tests/test_rag_notion.py`

- [ ] **Step 1: Create the package marker**

Create `app/rag/__init__.py` (empty file):

```python
```

- [ ] **Step 2: Write failing tests for the pure parsers**

Create `tests/test_rag_notion.py`:

```python
"""Unit tests for Notion block→text conversion and chunking (network-free)."""
from app.rag import notion


def _rich(text):
    return [{"type": "text", "text": {"content": text}, "plain_text": text}]


def test_blocks_to_text_extracts_paragraphs_and_headings():
    blocks = [
        {"type": "heading_1", "heading_1": {"rich_text": _rich("红利策略")}},
        {"type": "paragraph", "paragraph": {"rich_text": _rich("高股息逻辑。")}},
        {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": _rich("条目一")}},
        {"type": "image", "image": {}},  # non-text block → skipped, no crash
    ]
    text = notion.blocks_to_text(blocks)
    assert "红利策略" in text
    assert "高股息逻辑。" in text
    assert "条目一" in text


def test_blocks_to_text_empty_is_empty_string():
    assert notion.blocks_to_text([]) == ""


def test_chunk_text_splits_long_text_under_limit():
    para = "句子。" * 200  # 600 chars
    chunks = notion.chunk_text(para, max_chars=300)
    assert len(chunks) >= 2
    assert all(len(c) <= 300 for c in chunks)
    # No content lost.
    assert "".join(chunks).replace("\n", "") == para


def test_chunk_text_keeps_short_text_as_one_chunk():
    assert notion.chunk_text("短文本", max_chars=300) == ["短文本"]


def test_chunk_text_ignores_blank():
    assert notion.chunk_text("   \n  ", max_chars=300) == []
```

- [ ] **Step 3: Run tests — verify they fail**

Run: `cd /Users/chenmeng/PycharmProjects/StockBook && .venv/bin/python -m pytest tests/test_rag_notion.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.rag.notion'` or `AttributeError`.

- [ ] **Step 4: Implement `app/rag/notion.py`**

Create `app/rag/notion.py`:

```python
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
```

- [ ] **Step 5: Run tests — verify they pass**

Run: `cd /Users/chenmeng/PycharmProjects/StockBook && .venv/bin/python -m pytest tests/test_rag_notion.py -v`
Expected: PASS (5 passed).

Note on `test_chunk_text_splits_long_text_under_limit`: the input has no `\n`, so it hits the hard-split branch — chunks are exact `max_chars` slices and rejoin to the original. ✓

- [ ] **Step 6: Commit**

```bash
git add app/rag/__init__.py app/rag/notion.py tests/test_rag_notion.py
git commit -m "feat(rag): Notion block→text and chunking (pure, tested)"
```

---

## Task 5: Notion networking (fetch)

**Files:**
- Modify: `app/rag/notion.py`

These wrap `notion-client`; they are thin and not unit-tested (network), matching how `quotes._fetch_*` are only covered indirectly. Pure logic already tested in Task 4.

- [ ] **Step 1: Append fetch helpers to `app/rag/notion.py`**

Add at the end:

```python
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


def crawl_source(notion_id: str, kind: str) -> List[Dict[str, str]]:
    """Yield {page_id, url, title, text} for every page under a source.
    For a database, that's every row page; for a page, just itself."""
    page_ids = fetch_database_page_ids(notion_id) if kind == "database" else [notion_id]
    out: List[Dict[str, str]] = []
    for pid in page_ids:
        text = blocks_to_text(fetch_page_blocks(pid))
        if not text.strip():
            continue
        out.append({
            "page_id": pid,
            "url": notion_page_url(pid),
            "title": page_title(pid),
            "text": text,
        })
    return out
```

- [ ] **Step 2: Verify it imports (no network call)**

Run: `cd /Users/chenmeng/PycharmProjects/StockBook && .venv/bin/python -c "from app.rag import notion; print(notion.notion_page_url('abc-def'))"`
Expected: `https://www.notion.so/abcdef`

- [ ] **Step 3: Commit**

```bash
git add app/rag/notion.py
git commit -m "feat(rag): Notion fetch/crawl helpers"
```

---

## Task 6: Local embeddings

**Files:**
- Create: `app/rag/embed.py`
- Test: `tests/test_rag_store.py` (embedding smoke test included with store tests in Task 7)

- [ ] **Step 1: Implement `app/rag/embed.py`**

Create `app/rag/embed.py`:

```python
"""Local zero-cost embeddings via fastembed (ONNX, Chinese BGE).

The model is loaded lazily on first use and cached at module level (first call
downloads the model to fastembed's cache dir). No torch, no API cost.
"""
from __future__ import annotations

from typing import List, Optional

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
```

- [ ] **Step 2: Smoke-test the embedder (downloads model — may take a minute first run)**

Run: `cd /Users/chenmeng/PycharmProjects/StockBook && .venv/bin/python -c "
from app.rag import embed
v = embed.embed_one('红利策略 高股息')
print('dim', len(v))
assert len(v) > 100, len(v)
print('ok')
"`
Expected: prints a `dim` (384 for bge-small-zh) and `ok`. If the model download is blocked offline, note it and proceed — store tests in Task 7 use injected vectors and don't require the model.

- [ ] **Step 3: Commit**

```bash
git add app/rag/embed.py
git commit -m "feat(rag): local fastembed embedding wrapper"
```

---

## Task 7: Vector store + numpy cosine retrieval

**Files:**
- Create: `app/rag/store.py`
- Test: `tests/test_rag_store.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_rag_store.py`:

```python
"""Tests for the knowledge store: cosine ranking (pure) and DB round-trip."""
import json

from app.rag import store


def test_cosine_top_k_ranks_by_similarity():
    # 2-D vectors; query points along +x.
    query = [1.0, 0.0]
    rows = [
        (1, [1.0, 0.0]),    # identical → best
        (2, [0.0, 1.0]),    # orthogonal → worst
        (3, [0.9, 0.1]),    # close → second
    ]
    ranked = store.cosine_top_k(query, rows, k=2)
    assert [rid for rid, _ in ranked] == [1, 3]
    assert ranked[0][1] >= ranked[1][1]  # scores descending


def test_cosine_top_k_handles_zero_vector():
    # A zero vector must not raise (div-by-zero) — it just scores 0.
    ranked = store.cosine_top_k([1.0, 0.0], [(1, [0.0, 0.0])], k=1)
    assert ranked[0][0] == 1


def test_cosine_top_k_empty_rows():
    assert store.cosine_top_k([1.0, 0.0], [], k=3) == []


def test_replace_source_chunks_round_trip(client):
    # `client` fixture gives us a seeded temp DB; grab a session from it.
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

        # Replacing again wipes the old rows first (no accumulation).
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
    finally:
        db.close()
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `cd /Users/chenmeng/PycharmProjects/StockBook && .venv/bin/python -m pytest tests/test_rag_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.rag.store'`.

- [ ] **Step 3: Implement `app/rag/store.py`**

Create `app/rag/store.py`:

```python
"""Knowledge-chunk persistence + brute-force cosine retrieval (spec §5).

Embeddings are stored as JSON in a Text column and loaded into numpy at query
time. For a few thousand chunks this is sub-millisecond and needs no vector
extension. `cosine_top_k` is a pure function (unit-tested); `search` wires it
to the DB. To scale past ~tens of thousands of chunks, swap the body of
`search` for sqlite-vec — callers are unaffected.
"""
from __future__ import annotations

import json
from typing import Dict, List, Tuple

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


def search(db: Session, query_vec: List[float], k: int = None) -> List[Dict]:
    """Return the top-k chunks most similar to query_vec as dicts with text,
    notion_url, title_path, score."""
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
```

- [ ] **Step 4: Run tests — verify they pass**

Run: `cd /Users/chenmeng/PycharmProjects/StockBook && .venv/bin/python -m pytest tests/test_rag_store.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add app/rag/store.py tests/test_rag_store.py
git commit -m "feat(rag): SQLite chunk store + numpy cosine retrieval"
```

---

## Task 8: Holdings snapshot

**Files:**
- Create: `app/rag/snapshot.py`
- Test: `tests/test_rag_ask.py` (snapshot test included with ask tests in Task 9)

- [ ] **Step 1: Implement `app/rag/snapshot.py`**

Create `app/rag/snapshot.py`:

```python
"""Compact, text holdings snapshot for the RAG prompt (spec §6).

Reuses build_dashboard so the snapshot always matches what the user sees.
Kept short (class-level targets/current/deviation + top securities) to bound
prompt tokens. Amounts are included; this never runs in shared read-only mode
(the /api/rag/* endpoints 403 there).
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..services import build_dashboard


def holdings_snapshot(db: Session) -> str:
    payload = build_dashboard(db, readonly=False, hide_amounts=False)
    if not payload:
        return "（暂无持仓数据）"
    lines = []
    total = payload.get("total_assets")
    if total is not None:
        lines.append(f"总资产：约 {total:,.0f} 元")
    for ac in payload.get("asset_classes", []):
        cur = ac.get("current_weight")
        tgt = ac.get("target_weight")
        dev = ac.get("deviation")
        cur_s = f"{cur:.1f}%" if cur is not None else "—"
        dev_s = f"{dev:+.1f}%" if dev is not None else "—"
        names = "、".join(
            s["name"] for s in ac.get("securities", [])[:4] if s.get("shares")
        )
        line = f"- {ac['name']}：目标 {tgt:.0f}% / 当前 {cur_s}（偏离 {dev_s}）"
        if names:
            line += f"；持有：{names}"
        lines.append(line)
    return "\n".join(lines)
```

- [ ] **Step 2: Verify it runs against a seeded DB**

Run: `cd /Users/chenmeng/PycharmProjects/StockBook && STOCKBOOK_DATABASE_URL=sqlite:////tmp/rag_snap.db .venv/bin/python -c "
from app import database, seed
seed.init_db()  # creates schema + seeds the example strategy if empty
from app.rag.snapshot import holdings_snapshot
db = database.SessionLocal()
print(holdings_snapshot(db)[:200])
db.close()
" && rm -f /tmp/rag_snap.db`
Expected: a few lines like `总资产：…` and `- 沪深300：目标 …`. The keys used in `snapshot.py` (`total_assets`, `asset_classes[].name/target_weight/current_weight/deviation/securities`, `securities[].name/shares`) are confirmed to match `services.build_dashboard`'s payload (verified against `app/services.py:105-139`). Note: there is no public `seed.seed_example_data()` — use `seed.init_db()`.

- [ ] **Step 3: Commit**

```bash
git add app/rag/snapshot.py
git commit -m "feat(rag): compact holdings snapshot for prompt"
```

---

## Task 9: Prompt assembly + Claude call

**Files:**
- Create: `app/rag/ask.py`
- Test: `tests/test_rag_ask.py`

- [ ] **Step 1: Write failing tests for the pure prompt builder + snapshot**

Create `tests/test_rag_ask.py`:

```python
"""Tests for prompt assembly (pure) and the holdings snapshot."""
from app.rag import ask


def test_build_prompt_includes_question_snapshot_and_excerpts():
    chunks = [
        {"text": "红利策略偏好高股息。", "notion_url": "u1", "title_path": "策略/红利"},
        {"text": "中证500代表中小盘。", "notion_url": "u2", "title_path": "笔记/宽基"},
    ]
    prompt = ask.build_prompt("我该如何看红利?", chunks, "总资产：100万\n- 红利：目标20%")
    assert "我该如何看红利?" in prompt
    assert "红利策略偏好高股息。" in prompt
    assert "u1" in prompt                  # source link present for citation
    assert "总资产：100万" in prompt        # snapshot injected


def test_build_prompt_truncates_long_excerpt():
    long_text = "字" * 5000
    prompt = ask.build_prompt("q", [{"text": long_text, "notion_url": "u", "title_path": "t"}], "")
    # Each excerpt capped at RAG_EXCERPT_CHARS (default 800); not the full 5000.
    assert prompt.count("字") <= 1000


def test_build_prompt_no_chunks_states_no_context():
    prompt = ask.build_prompt("q", [], "snap")
    assert "没有" in prompt or "未找到" in prompt  # instructs model: no KB hit


def test_holdings_snapshot_no_strategy(client):
    # Fresh client is seeded; but verify snapshot is a string regardless.
    from app import database
    from app.rag.snapshot import holdings_snapshot
    db = database.SessionLocal()
    try:
        assert isinstance(holdings_snapshot(db), str)
    finally:
        db.close()
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `cd /Users/chenmeng/PycharmProjects/StockBook && .venv/bin/python -m pytest tests/test_rag_ask.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.rag.ask'`.

- [ ] **Step 3: Implement `app/rag/ask.py`**

Create `app/rag/ask.py`:

```python
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
```

- [ ] **Step 4: Run tests — verify they pass**

Run: `cd /Users/chenmeng/PycharmProjects/StockBook && .venv/bin/python -m pytest tests/test_rag_ask.py -v`
Expected: PASS (4 passed). (Only `build_prompt` and `holdings_snapshot` are exercised; `answer()` is covered via a mock in Task 11.)

- [ ] **Step 5: Commit**

```bash
git add app/rag/ask.py tests/test_rag_ask.py
git commit -m "feat(rag): prompt assembly + Claude answer (cached system prompt)"
```

---

## Task 10: Sync orchestration + rate limiter

**Files:**
- Create: `app/rag/limiter.py`
- Modify: `app/rag/store.py` (add `sync_source`)
- Test: `tests/test_rag_store.py` (add limiter + sync tests)

- [ ] **Step 1: Write failing tests (append to `tests/test_rag_store.py`)**

Add at the end of `tests/test_rag_store.py`:

```python
def test_limiter_counts_and_caps():
    from app.rag import limiter
    lim = limiter.DailyLimiter(limit=2)
    assert lim.allow("2026-05-31") is True
    assert lim.allow("2026-05-31") is True
    assert lim.allow("2026-05-31") is False   # third call same day → blocked
    assert lim.allow("2026-06-01") is True     # new day resets
    assert lim.remaining("2026-06-01") == 1


def test_sync_source_embeds_and_stores(client, monkeypatch):
    from app import database
    from app.models import NotionSource, KnowledgeChunk
    from app.rag import store, notion, embed

    # Stub network + embedding so the test is offline & deterministic.
    monkeypatch.setattr(notion, "crawl_source",
                        lambda nid, kind: [
                            {"page_id": "p1", "url": "u1", "title": "策略",
                             "text": "红利逻辑\n高股息偏好"},
                        ])
    monkeypatch.setattr(embed, "embed_texts",
                        lambda texts: [[0.1, 0.2] for _ in texts])

    db = database.SessionLocal()
    try:
        src = NotionSource(notion_id="nid", title="策略", kind="page")
        db.add(src); db.commit(); db.refresh(src)
        n = store.sync_source(db, src)
        db.commit()
        assert n >= 1
        rows = db.query(KnowledgeChunk).filter_by(source_id=src.id).all()
        assert len(rows) == n
        assert src.last_synced_at is not None
    finally:
        db.close()
```

- [ ] **Step 2: Run — verify they fail**

Run: `cd /Users/chenmeng/PycharmProjects/StockBook && .venv/bin/python -m pytest tests/test_rag_store.py::test_limiter_counts_and_caps tests/test_rag_store.py::test_sync_source_embeds_and_stores -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.rag.limiter'` / `AttributeError: ... 'sync_source'`.

- [ ] **Step 3: Implement `app/rag/limiter.py`**

Create `app/rag/limiter.py`:

```python
"""In-process daily call counter for cost protection (spec §7.2).

Single-user local app, single process → an in-memory counter is sufficient
(resets on restart, which is acceptable). Keyed by date string so a new day
resets automatically.
"""
from __future__ import annotations

from typing import Dict


class DailyLimiter:
    def __init__(self, limit: int):
        self.limit = limit
        self._counts: Dict[str, int] = {}

    def allow(self, day: str) -> bool:
        """Record one call for `day`; return False if it exceeds the limit."""
        used = self._counts.get(day, 0)
        if used >= self.limit:
            return False
        self._counts[day] = used + 1
        return True

    def remaining(self, day: str) -> int:
        return max(0, self.limit - self._counts.get(day, 0))
```

- [ ] **Step 4: Add `sync_source` to `app/rag/store.py`**

Append to `app/rag/store.py` (add `import datetime as dt` at the top with the other imports, and `from . import embed, notion` — but to avoid a circular import, import them lazily inside the function):

```python
def sync_source(db: Session, source: NotionSource) -> int:
    """Crawl a Notion source, embed its pages' chunks, and replace stored
    chunks for it (delete-and-rebuild). Returns chunk count. Caller commits."""
    import datetime as dt
    from . import embed, notion

    pages = notion.crawl_source(source.notion_id, source.kind)
    records = []
    for page in pages:
        for piece in notion.chunk_text(page["text"]):
            records.append({
                "page_id": page["page_id"], "url": page["url"],
                "title_path": page.get("title", ""), "text": piece,
            })
    vectors = embed.embed_texts([r["text"] for r in records]) if records else []
    for r, v in zip(records, vectors):
        r["embedding"] = v
    n = replace_source_chunks(db, source, records)
    source.last_synced_at = dt.datetime.now()
    return n
```

- [ ] **Step 5: Run — verify they pass**

Run: `cd /Users/chenmeng/PycharmProjects/StockBook && .venv/bin/python -m pytest tests/test_rag_store.py -v`
Expected: PASS (all, including the two new ones).

- [ ] **Step 6: Commit**

```bash
git add app/rag/limiter.py app/rag/store.py tests/test_rag_store.py
git commit -m "feat(rag): sync orchestration + daily rate limiter"
```

---

## Task 11: API router with guards

**Files:**
- Create: `app/routers/rag.py`
- Modify: `app/schemas.py`, `main.py`
- Test: `tests/test_rag_api.py`

- [ ] **Step 1: Add schemas to `app/schemas.py`**

Append to `app/schemas.py`:

```python
class AskRequest(BaseModel):
    question: str = Field(..., min_length=1)


class NotionSourceCreate(BaseModel):
    notion_id: str = Field(..., min_length=1)
    title: Optional[str] = ""
    kind: str = "page"  # "page" | "database"
```

- [ ] **Step 2: Write failing API tests**

Create `tests/test_rag_api.py`:

```python
"""API tests for the RAG router: master switch, read-only 403, rate limit."""
import importlib


def _enable_rag(monkeypatch):
    from app import config
    monkeypatch.setattr(config, "RAG_ENABLED", True)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")


def test_status_reports_disabled_by_default(client):
    r = client.get("/api/rag/status")
    assert r.status_code == 200
    assert r.json()["enabled"] is False


def test_ask_blocked_when_disabled(client):
    r = client.post("/api/rag/ask", json={"question": "hi"})
    assert r.status_code == 403
    assert "未启用" in r.json()["detail"]


def test_ask_blocked_in_readonly(client, monkeypatch):
    _enable_rag(monkeypatch)
    from app import config
    monkeypatch.setattr(config, "READONLY", True)
    r = client.post("/api/rag/ask", json={"question": "hi"})
    assert r.status_code == 403


def test_ask_calls_answer_and_returns_payload(client, monkeypatch):
    _enable_rag(monkeypatch)
    from app.rag import ask
    monkeypatch.setattr(ask, "answer",
                        lambda db, q: {"answer": "摘要…", "citations": []})
    r = client.post("/api/rag/ask", json={"question": "红利怎么看?"})
    assert r.status_code == 200
    assert r.json()["answer"] == "摘要…"


def test_ask_rate_limited(client, monkeypatch):
    _enable_rag(monkeypatch)
    from app import config
    from app.rag import ask
    from app.routers import rag as rag_router
    monkeypatch.setattr(config, "RAG_DAILY_LIMIT", 1)
    # Reset the shared limiter to honour the patched limit.
    monkeypatch.setattr(rag_router, "_limiter",
                        rag_router.limiter.DailyLimiter(config.RAG_DAILY_LIMIT))
    monkeypatch.setattr(ask, "answer", lambda db, q: {"answer": "x", "citations": []})
    assert client.post("/api/rag/ask", json={"question": "1"}).status_code == 200
    r = client.post("/api/rag/ask", json={"question": "2"})
    assert r.status_code == 429
```

- [ ] **Step 3: Run — verify they fail**

Run: `cd /Users/chenmeng/PycharmProjects/StockBook && .venv/bin/python -m pytest tests/test_rag_api.py -v`
Expected: FAIL — 404s (router not mounted) / import errors.

- [ ] **Step 4: Implement `app/routers/rag.py`**

Create `app/routers/rag.py`:

```python
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


@router.post("/sync", dependencies=[Depends(_require_enabled)])
def sync(db: Session = Depends(get_db)):
    """Re-sync all sources (delete-and-rebuild). Embedding is local (free);
    no LLM call here (spec §7.2)."""
    results = []
    for src in db.query(NotionSource).all():
        try:
            n = store.sync_source(db, src)
            db.commit()
            results.append({"source_id": src.id, "title": src.title, "chunks": n})
        except Exception as e:  # one source failing shouldn't abort the rest
            db.rollback()
            results.append({"source_id": src.id, "title": src.title, "error": str(e)})
    return {"results": results, "chunk_count": store.chunk_count(db)}
```

- [ ] **Step 5: Register the router in `main.py`**

In `main.py` (verified contents), change the import line:

```python
from app.routers import api, pages
```
to:
```python
from app.routers import api, pages, rag
```

and add, right after `app.include_router(pages.router)`:

```python
app.include_router(rag.router)
```

- [ ] **Step 6: Run — verify they pass**

Run: `cd /Users/chenmeng/PycharmProjects/StockBook && .venv/bin/python -m pytest tests/test_rag_api.py -v`
Expected: PASS (5 passed).

- [ ] **Step 7: Run the FULL suite (no regressions)**

Run: `cd /Users/chenmeng/PycharmProjects/StockBook && .venv/bin/python -m pytest -q`
Expected: all pass (existing + new).

- [ ] **Step 8: Commit**

```bash
git add app/routers/rag.py app/schemas.py main.py tests/test_rag_api.py
git commit -m "feat(rag): /api/rag router with master switch, readonly 403, rate limit"
```

---

## Task 12: Frontend floating widget

**Files:**
- Create: `templates/_rag_widget.html`, `static/js/rag.js`
- Modify: `templates/index.html`

Follow the existing visual system (paper-tone palette, the CSS vars already in `static/css/style.css`). The widget calls `/api/rag/status` on load; if `enabled` is false (disabled or read-only) it renders nothing.

- [ ] **Step 1: Create `templates/_rag_widget.html`**

```html
<!-- Floating RAG Q&A widget. Hidden until /api/rag/status says enabled. -->
<div id="rag-fab" class="rag-fab" hidden title="笔记问答">问</div>
<div id="rag-panel" class="rag-panel" hidden>
  <div class="rag-head">
    <span>笔记问答</span>
    <div class="rag-head-actions">
      <button id="rag-sync" class="rag-btn" title="重新同步知识库">同步</button>
      <button id="rag-close" class="rag-btn" title="关闭">×</button>
    </div>
  </div>
  <div id="rag-status" class="rag-status"></div>
  <div id="rag-log" class="rag-log"></div>
  <form id="rag-form" class="rag-form">
    <input id="rag-input" class="rag-input" type="text" placeholder="问问你的投资笔记…" autocomplete="off">
    <button type="submit" class="rag-btn rag-send">问</button>
  </form>
</div>
```

- [ ] **Step 2: Create `static/js/rag.js`**

```javascript
// Floating RAG Q&A widget. Self-contained; no-op when the feature is disabled.
(function () {
  const fab = document.getElementById('rag-fab');
  const panel = document.getElementById('rag-panel');
  if (!fab || !panel) return;

  const log = document.getElementById('rag-log');
  const statusEl = document.getElementById('rag-status');
  const form = document.getElementById('rag-form');
  const input = document.getElementById('rag-input');
  const syncBtn = document.getElementById('rag-sync');

  function add(role, html) {
    const div = document.createElement('div');
    div.className = 'rag-msg rag-' + role;
    div.innerHTML = html;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }

  function esc(s) {
    return (s || '').replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
  }

  async function refreshStatus() {
    try {
      const r = await fetch('/api/rag/status');
      const s = await r.json();
      if (!s.enabled) { fab.hidden = true; panel.hidden = true; return; }
      fab.hidden = false;
      statusEl.textContent =
        `模型 ${s.model} · 片段 ${s.chunk_count} · 今日剩余 ${s.remaining_today}/${s.daily_limit}`;
    } catch (e) { fab.hidden = true; }
  }

  fab.addEventListener('click', () => { panel.hidden = false; fab.hidden = true; input.focus(); });
  document.getElementById('rag-close').addEventListener('click', () => {
    panel.hidden = true; fab.hidden = false;
  });

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (!q) return;
    add('user', esc(q));
    input.value = '';
    add('bot', '<em>思考中…</em>');
    const pending = log.lastChild;
    try {
      const r = await fetch('/api/rag/ask', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: q }),
      });
      const data = await r.json();
      if (!r.ok) { pending.innerHTML = '<em>' + esc(data.detail || '出错了') + '</em>'; return; }
      let html = esc(data.answer).replace(/\n/g, '<br>');
      if (data.citations && data.citations.length) {
        html += '<div class="rag-cites">来源:';
        data.citations.forEach((c, i) => {
          html += ` <a href="${esc(c.notion_url)}" target="_blank" rel="noopener">[${i + 1}] ${esc(c.title_path)}</a>`;
        });
        html += '</div>';
      }
      pending.innerHTML = html;
    } catch (e) { pending.innerHTML = '<em>网络错误</em>'; }
    refreshStatus();
  });

  syncBtn.addEventListener('click', async () => {
    syncBtn.disabled = true; syncBtn.textContent = '同步中…';
    try {
      const r = await fetch('/api/rag/sync', { method: 'POST' });
      const data = await r.json();
      add('bot', r.ok ? `<em>同步完成,共 ${data.chunk_count} 个片段。</em>`
                      : `<em>${esc(data.detail || '同步失败')}</em>`);
    } catch (e) { add('bot', '<em>同步失败</em>'); }
    syncBtn.disabled = false; syncBtn.textContent = '同步';
    refreshStatus();
  });

  refreshStatus();
})();
```

- [ ] **Step 3: Add widget styles**

Append to `static/css/style.css` (reuse existing CSS vars — `--card`, `--edge`, `--accent`, `--ink`, `--r`):

```css
/* ---------- RAG Q&A widget ---------- */
.rag-fab{position:fixed;right:24px;bottom:24px;z-index:50;width:52px;height:52px;border-radius:16px;
  background:linear-gradient(150deg,var(--accent),#8a3416);color:#FBF1E6;font-family:var(--font-display);
  font-size:22px;display:grid;place-items:center;cursor:pointer;border:none;
  box-shadow:0 6px 18px rgba(168,67,31,.3)}
.rag-panel{position:fixed;right:24px;bottom:24px;z-index:51;width:360px;max-width:calc(100vw - 32px);
  height:480px;max-height:calc(100vh - 48px);display:flex;flex-direction:column;
  background:var(--card);border:1px solid var(--edge);border-radius:var(--r);
  box-shadow:0 18px 50px -18px rgba(60,40,20,.5)}
.rag-head{display:flex;justify-content:space-between;align-items:center;padding:12px 14px;
  border-bottom:1px solid var(--edge);font-family:var(--font-display);font-size:16px}
.rag-head-actions{display:flex;gap:6px}
.rag-btn{border:1px solid var(--edge-strong);background:var(--card-2);border-radius:8px;
  padding:4px 10px;cursor:pointer;color:var(--ink-2);font-size:13px}
.rag-btn:hover{border-color:var(--accent);color:var(--accent)}
.rag-status{padding:6px 14px;font-size:11.5px;color:var(--ink-3);border-bottom:1px solid var(--edge)}
.rag-log{flex:1;overflow-y:auto;padding:12px 14px;display:flex;flex-direction:column;gap:10px}
.rag-msg{font-size:13.5px;line-height:1.5;padding:8px 11px;border-radius:10px;max-width:90%}
.rag-user{align-self:flex-end;background:var(--accent-soft);color:var(--ink)}
.rag-bot{align-self:flex-start;background:var(--card-2);color:var(--ink-2)}
.rag-cites{margin-top:6px;font-size:11.5px;color:var(--ink-3)}
.rag-cites a{color:var(--accent);text-decoration:none}
.rag-form{display:flex;gap:8px;padding:10px 12px;border-top:1px solid var(--edge)}
.rag-input{flex:1;border:1px solid var(--edge-strong);border-radius:9px;padding:8px 11px;
  font-family:var(--font-ui);font-size:13.5px;background:var(--card-2);color:var(--ink)}
.rag-send{background:var(--accent);color:#FBF1E6;border-color:var(--accent)}
```

- [ ] **Step 4: Include the widget in `index.html`**

In `templates/index.html`, the existing script includes are (verified) at lines 184–185:

```html
<script src="/static/js/common.js"></script>
<script src="/static/js/app.js"></script>
```

Add immediately after line 185 (the project uses absolute `/static/...` URLs — confirmed, no `url_for`):

```html
{% include "_rag_widget.html" %}
<script src="/static/js/rag.js"></script>
```

- [ ] **Step 5: Manual smoke test**

Run: `cd /Users/chenmeng/PycharmProjects/StockBook && STOCKBOOK_DATABASE_URL=sqlite:////tmp/rag_ui.db STOCKBOOK_RAG_ENABLED=1 ANTHROPIC_API_KEY=dummy .venv/bin/python -m uvicorn main:app --port 8011` (background), then open `http://127.0.0.1:8011/`.
Expected: a "问" bubble appears bottom-right; clicking opens the panel; status line shows model + 片段 0 + 今日剩余 50/50. Stop the server and `rm -f /tmp/rag_ui.db` after. (No real Notion/Claude needed to see the widget render; asking will 503 without a real key, which is fine for this UI check.)

- [ ] **Step 6: Commit**

```bash
git add templates/_rag_widget.html static/js/rag.js static/css/style.css templates/index.html
git commit -m "feat(rag): floating Q&A widget (hidden when disabled/readonly)"
```

---

## Task 13: Documentation

**Files:**
- Modify: `docs/architecture.md`

Per architecture.md §8 ("新增功能 = 同时更新本文档") and the project memory, every feature updates the architecture doc.

- [ ] **Step 1: Add a decision, the API entries, and a changelog line**

In `docs/architecture.md`:

1. Add to **§4 关键决策** a new numbered item:

```markdown
15. **RAG 问答(Phase 2)**:独立 `app/rag/` 子包(notion 解析/抓取、fastembed 本地向量化、numpy 暴力余弦检索、prompt 组装 + Claude)。两张新表 `NotionSource`/`KnowledgeChunk`(向量以 JSON 存 Text 列,不依赖向量扩展,保持单文件可打包)。三重成本/安全护栏:总开关 `STOCKBOOK_RAG_ENABLED`、只读模式 403、每日限流(默认 50,`STOCKBOOK_RAG_DAILY_LIMIT` 可改);key 仅后端、走 .env、不下发前端;同步阶段不调 LLM(仅 Notion + 本地 embed)。默认模型 Haiku(`STOCKBOOK_RAG_MODEL` 可切)。检索接口封装在 `store.search`,日后上万片段可平滑换 sqlite-vec。
```

2. Add to **§5 JSON API 一览**:

```markdown
- RAG 问答:`GET /api/rag/status`(始终可用,前端据此决定是否显示问答窗)、`POST /api/rag/ask`(三重护栏:总开关/只读/限流)、`POST /api/rag/sync`(删旧重建,不调 LLM)、`POST/DELETE /api/rag/sources`。
```

3. Add to **§7 功能日志** a dated line:

```markdown
- **2026-05-31** Notion RAG 问答(Phase 2):`app/rag/`(notion/embed/store/ask/snapshot/limiter)+ `routers/rag.py` + 浮动问答小窗;手动「同步」删旧重建,fastembed 本地向量化,numpy 余弦 top-k,持仓快照注入 prompt,Claude(默认 Haiku)摘要 + 原文引用 + Notion 链接;总开关 + 只读 403 + 每日限流三重护栏;`.env` 入 `.gitignore`。设计见 `docs/superpowers/specs/2026-05-31-stockbook-rag-qa-design.md`。
```

- [ ] **Step 2: Commit**

```bash
git add docs/architecture.md
git commit -m "docs: record RAG Q&A architecture, API, and changelog"
```

---

## Task 14: Final verification

- [ ] **Step 1: Full test suite**

Run: `cd /Users/chenmeng/PycharmProjects/StockBook && .venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 2: Confirm feature is OFF by default**

Run: `cd /Users/chenmeng/PycharmProjects/StockBook && STOCKBOOK_DATABASE_URL=sqlite:////tmp/rag_off.db .venv/bin/python -c "
from fastapi.testclient import TestClient
from app.main import app
with TestClient(app) as c:
    assert c.get('/api/rag/status').json()['enabled'] is False
    assert c.post('/api/rag/ask', json={'question':'x'}).status_code == 403
print('feature OFF by default ✓')
" && rm -f /tmp/rag_off.db`
Expected: `feature OFF by default ✓`

- [ ] **Step 3: Confirm `.env` is gitignored**

Run: `cd /Users/chenmeng/PycharmProjects/StockBook && git check-ignore .env && echo ".env ignored ✓"`
Expected: `.env` then `.env ignored ✓`. (Already added in a prior step; this confirms key files won't be committed.)

- [ ] **Step 4: End-to-end manual test (optional, needs real keys)**

With a real `.env` (`NOTION_TOKEN`, `ANTHROPIC_API_KEY`, `STOCKBOOK_RAG_ENABLED=1`) and the integration shared to a test page: add a source via `POST /api/rag/sources`, click 同步, then ask a question in the widget. Confirm a summary + cited Notion links come back.

---

## Self-Review Notes (completed during planning)

- **Spec coverage:** §3 data flow → Tasks 4–10; §4 tables → Task 3; §5 numpy cosine → Task 7; §6 holdings snapshot (simple, no tool-calling) → Tasks 8–9; §7 security/cost (master switch, readonly 403, daily limit, key handling, local embed, Haiku) → Tasks 2, 9, 11; §8 floating widget + readonly hide → Task 12; §9 module layout → Tasks 4–11; §10 API → Task 11; §11 fallbacks → Task 11 (guards) + ask.py (no-chunk message); §12 tests → every task; `.env` gitignore (§7.1) → already applied + verified Task 14.
- **Type consistency:** chunk dict keys (`page_id`, `url`, `title_path`/`title`, `text`, `embedding`) flow consistently `notion.crawl_source` → `store.sync_source` → `replace_source_chunks`; `search` returns `{id,text,notion_url,title_path,score}` consumed by `ask.build_prompt` and the router citations.
- **Verified against the real code:** (1) `snapshot.py` keys match `services.build_dashboard` (`app/services.py:105-139`); (2) `main.py` router include is exact (`from app.routers import api, pages` → add `rag`); (3) `index.html` uses absolute `/static/...` includes at lines 184-185; (4) `RAG_MODEL` default `claude-haiku-4-5-20251001` matches the env knowledge in the architecture doc; (5) `seed.init_db()` is the public seeding entrypoint (no `seed_example_data`). Dependency versions are floors — pin to what installs cleanly.
