# 数据源接口化(子项目 B)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把行情源 / embedding / 检索后端三处从「逻辑与实现缠在一起」重构成「各自的 `Protocol` 接口 + 实现类 + 注册/选择」,纯重构、对外行为逐字不变。

**Architecture:** 三个**独立**接口(`QuoteSource`/`Embedder`/`Retriever`),同一种做法:接口用 `typing.Protocol`(结构化、免继承),现有实现包成类,行情源用注册表 dict、embedding/检索用 `get_*()` 选择器。被消费的模块级公共函数全部保留为兼容垫片,委托给实现。

**Tech Stack:** Python 3.9、`typing.Protocol`、pytest、httpx、numpy、fastembed。

**Spec:** `docs/superpowers/specs/2026-06-01-stockbook-datasource-interfaces-design.md`

**前置约定(贯穿全程):**
- 已在分支 `feat/datasource-interfaces`;commit 末尾附 `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`。
- venv:`source .venv/bin/activate`。**绝不碰 `stockbook.db`**。
- Python 3.9:`typing.Optional/List`,不用 `X | None`。
- **重构铁律**:现有 111 个测试是回归安全网。任何一步做完都跑 `pytest -q`,必须保持全绿(行为不变的硬证据)。
- **模块化提交**:四个模块各一个 commit(行情 / embedding / 检索 / 文档),不要每个微步一个 commit。

---

## 行为保留清单(这些公共名字签名/语义不可变)

`quotes`: `parse_tencent/parse_sina/parse_eastmoney`、`to_qq_symbol`、`to_em_secid`、`is_trading_session`、`fetch_quotes(items, sources=None)`、`LAST_SOURCE`。
`embed`: `embed_texts(texts)`、`embed_one(text)`。
`store`: `cosine_top_k`、`search(db, query_vec, k=None)`、`sync_source`、`replace_source_chunks`、`chunk_count`。
消费方 `routers/api.py`、`routers/rag.py`、`services.py` **一行不改**。

---

## Task A:行情源接口化(`QuoteSource`)

**Files:**
- Modify: `app/quotes.py`(加接口/实现类/注册表,改 `fetch_quotes`,删 `_FETCHERS` 与三个 `_fetch_*`)
- Modify: `tests/test_quotes.py`(改写两个 failover 测试为注册表 + FakeQuoteSource,加扩展性测试)

- [ ] **Step 1: 在 `app/quotes.py` 顶部 typing 导入加入 `Protocol`**

把现有 `from typing import ...` 行补上 `Protocol`(若已 import typing 名称,追加 `Protocol` 即可)。

- [ ] **Step 2: 加接口与三个实现类 + 注册表**

在三个 `parse_*` 函数之后、`_get` 之后(即原 `_fetch_*` 位置)替换为:把原 `_fetch_tencent/_fetch_sina/_fetch_eastmoney` 三个函数**删除**,新增:

```python
# --------------------------------------------------------------------------- #
# Quote sources — each is a QuoteSource: a named backend that maps (code,
# market) pairs to {code: {"price","name"}}, raising httpx.HTTPError on
# transport failure. parse_* stay module-level pure helpers (unit-tested).
# --------------------------------------------------------------------------- #
class QuoteSource(Protocol):
    name: str
    def fetch(self, items: List[Tuple[str, str]]) -> Dict[str, dict]: ...


class TencentSource:
    name = "tencent"
    def fetch(self, items: List[Tuple[str, str]]) -> Dict[str, dict]:
        syms = _qq_symbols(items)
        if not syms:
            return {}
        r = _get(_TENCENT_URL + ",".join(syms), {"Referer": "https://finance.qq.com"})
        return parse_tencent(r.content.decode("gbk", errors="ignore"))


class SinaSource:
    name = "sina"
    def fetch(self, items: List[Tuple[str, str]]) -> Dict[str, dict]:
        syms = _qq_symbols(items)
        if not syms:
            return {}
        r = _get(_SINA_URL + ",".join(syms), {"Referer": "https://finance.sina.com.cn"})
        return parse_sina(r.content.decode("gbk", errors="ignore"))


class EastmoneySource:
    name = "eastmoney"
    def fetch(self, items: List[Tuple[str, str]]) -> Dict[str, dict]:
        secids = [s for s in (to_em_secid(c, m) for c, m in items) if s]
        if not secids:
            return {}
        r = _get(_EASTMONEY_URL + ",".join(secids))
        return parse_eastmoney(r.text)


# Registry: name -> source. Replaces the old _FETCHERS function map.
QUOTE_SOURCES: Dict[str, QuoteSource] = {
    s.name: s for s in (TencentSource(), SinaSource(), EastmoneySource())
}
```

- [ ] **Step 3: 改 `fetch_quotes` 用注册表**

把现有 `_FETCHERS = {...}` 删除,`fetch_quotes` 主体改为遍历 `QUOTE_SOURCES` 并调 `.fetch`:

```python
def fetch_quotes(items: Iterable[Tuple[str, str]],
                 sources: Optional[List[str]] = None) -> Dict[str, dict]:
    """Fetch live quotes for (code, market) pairs, trying sources in order.
    Returns {code: {"price","name"}} from the first source that responds with
    data. Raises httpx.HTTPError only if EVERY tried source failed at transport
    level. Sets module-level LAST_SOURCE to the winning source name."""
    global LAST_SOURCE
    items = list(items)
    chain = sources if sources is not None else list(config.QUOTE_SOURCES)
    last_err: Optional[httpx.HTTPError] = None
    for name in chain:
        source = QUOTE_SOURCES.get(name)
        if source is None:
            continue
        try:
            out = source.fetch(items)
        except httpx.HTTPError as e:
            last_err = e
            continue
        if out:
            LAST_SOURCE = name
            return out
    if last_err is not None:
        raise last_err
    LAST_SOURCE = None
    return {}
```

- [ ] **Step 4: 改写 `tests/test_quotes.py` 的两个 failover 测试 + 加扩展性测试**

把现有的 `test_fetch_quotes_failover` 与 `test_fetch_quotes_all_sources_fail_raises` 两个函数整体替换为:

```python
class FakeQuoteSource:
    """A QuoteSource for tests — no network. Returns a canned result or raises."""
    def __init__(self, name, result=None, error=None):
        self.name = name
        self._result = result or {}
        self._error = error

    def fetch(self, items):
        if self._error is not None:
            raise self._error
        return self._result


def test_fetch_quotes_failover(monkeypatch):
    bad = FakeQuoteSource("tencent", error=httpx.ConnectError("down"))
    good = FakeQuoteSource("sina", result={"510300": {"price": 1.0, "name": "x"}})
    monkeypatch.setitem(quotes.QUOTE_SOURCES, "tencent", bad)
    monkeypatch.setitem(quotes.QUOTE_SOURCES, "sina", good)
    out = quotes.fetch_quotes([("510300", "CN")], sources=["tencent", "sina"])
    assert out["510300"]["price"] == 1.0
    assert quotes.LAST_SOURCE == "sina"


def test_fetch_quotes_all_sources_fail_raises(monkeypatch):
    bad = FakeQuoteSource("tencent", error=httpx.ConnectError("down"))
    monkeypatch.setitem(quotes.QUOTE_SOURCES, "tencent", bad)
    monkeypatch.setitem(quotes.QUOTE_SOURCES, "sina",
                        FakeQuoteSource("sina", error=httpx.ConnectError("down")))
    with pytest.raises(httpx.HTTPError):
        quotes.fetch_quotes([("510300", "CN")], sources=["tencent", "sina"])


def test_fetch_quotes_supports_new_registered_source(monkeypatch):
    # 扩展性:加一个新源 = 注册一个对象,fetch_quotes 无需改动即可用它。
    custom = FakeQuoteSource("custom", result={"510300": {"price": 9.9, "name": "c"}})
    monkeypatch.setitem(quotes.QUOTE_SOURCES, "custom", custom)
    out = quotes.fetch_quotes([("510300", "CN")], sources=["custom"])
    assert out["510300"]["price"] == 9.9
    assert quotes.LAST_SOURCE == "custom"
```

确认文件顶部已 `import pytest` 与 `import httpx`(原 failover 测试已用到 `httpx`,如缺则补 import)。

- [ ] **Step 5: 跑行情测试 + 全套**

Run: `source .venv/bin/activate && pytest tests/test_quotes.py -q && pytest -q`
Expected: `test_quotes.py` 全绿(含 3 个 failover/扩展测试);全套 111+1(新)= 112 通过。若 `_FETCHERS` 还被别处引用导致报错,grep `grep -rn "_FETCHERS\|_fetch_tencent\|_fetch_sina\|_fetch_eastmoney" app/ tests/` 应只剩历史无引用——清干净。

- [ ] **Step 6: Commit(模块 A 一个)**

```bash
git add app/quotes.py tests/test_quotes.py
git commit -m "refactor(quotes): QuoteSource Protocol + source classes + registry

Replace the _FETCHERS function map with a QUOTE_SOURCES registry of
TencentSource/SinaSource/EastmoneySource (each a QuoteSource Protocol).
fetch_quotes/LAST_SOURCE/parse_* unchanged in behavior; failover tests
now use a network-free FakeQuoteSource and cover registering a new source.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task B:向量化接口化(`Embedder`)

**Files:**
- Modify: `app/rag/embed.py`(加 `Embedder` 接口 + `FastembedEmbedder` + `get_embedder`,模块函数改委托)
- Modify: `tests/test_rag_embed.py`(新建,小测)

- [ ] **Step 1: 重写 `app/rag/embed.py`**

整文件替换为:

```python
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
```

- [ ] **Step 2: 确认没有别处引用旧的 `embed._model`/`embed._get_model`**

Run: `grep -rn "_get_model\|embed\._model" app/ tests/`
Expected: 无结果(它们已收进 `FastembedEmbedder`)。若有引用,说明遗漏,需处理。

- [ ] **Step 3: 新建 `tests/test_rag_embed.py`**

```python
"""Embedder interface: shim behavior + swappability without loading fastembed."""
from app.rag import embed


def test_embed_texts_empty_returns_empty_without_model():
    # 空输入不该触发模型加载(惰性);保持原 embed_texts([]) == [] 行为。
    assert embed.embed_texts([]) == []


def test_get_embedder_is_singleton():
    assert embed.get_embedder() is embed.get_embedder()
    assert isinstance(embed.get_embedder(), embed.FastembedEmbedder)


def test_module_shims_delegate_to_get_embedder(monkeypatch):
    class FakeEmbedder:
        def embed_texts(self, texts):
            return [[1.0, 2.0] for _ in texts]
        def embed_one(self, text):
            return [9.0]
    monkeypatch.setattr(embed, "get_embedder", lambda: FakeEmbedder())
    assert embed.embed_texts(["a", "b"]) == [[1.0, 2.0], [1.0, 2.0]]
    assert embed.embed_one("q") == [9.0]
```

- [ ] **Step 4: 跑测试 + 全套**

Run: `source .venv/bin/activate && pytest tests/test_rag_embed.py tests/test_rag_ask.py -q && pytest -q`
Expected: 新测试全绿;`test_rag_ask.py`(monkeypatch `embed.embed_one`)仍绿;全套通过(112+3=115)。

- [ ] **Step 5: Commit(模块 B 一个)**

```bash
git add app/rag/embed.py tests/test_rag_embed.py
git commit -m "refactor(rag/embed): Embedder Protocol + FastembedEmbedder + get_embedder

Wrap the lazy fastembed model in a FastembedEmbedder class behind an
Embedder Protocol, selected via get_embedder(). Module-level embed_texts/
embed_one become shims so call sites and tests are unchanged; a FakeEmbedder
test proves the seam works without loading the model.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task C:检索接口化(`Retriever`)

**Files:**
- Modify: `app/rag/store.py`(加 `Retriever` 接口 + `NumpyCosineRetriever` + `get_retriever`,`search` 改委托)
- Modify: `tests/test_rag_store.py`(加 retriever 委托/可换测试)

- [ ] **Step 1: 在 `app/rag/store.py` 顶部 typing 导入加入 `Protocol`**

把现有 `from typing import ...` 补上 `Protocol`(若无 typing import 行则新增 `from typing import Protocol`)。

- [ ] **Step 2: 把 `search` 主体搬进 `NumpyCosineRetriever`,模块 `search` 改委托**

将现有的模块级 `def search(db, query_vec, k=None): ...` 整个替换为以下接口 + 实现类 + 选择器 + 垫片(`cosine_top_k`、`_embedding_index`、`_embed_cache` 保持原样不动):

```python
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
```

> 注:`sync_source` 不变——它对 embedding 的调用走 `embed.embed_texts`(已是垫片→`get_embedder()`),无需在本任务改。

- [ ] **Step 3: 在 `tests/test_rag_store.py` 末尾追加委托/可换测试**

```python
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


def test_get_retriever_is_singleton():
    from app.rag import store
    assert store.get_retriever() is store.get_retriever()
    assert isinstance(store.get_retriever(), store.NumpyCosineRetriever)
```

- [ ] **Step 4: 跑测试 + 全套**

Run: `source .venv/bin/activate && pytest tests/test_rag_store.py -q && pytest -q`
Expected: `test_rag_store.py` 全绿(含 `cosine_top_k` 原测试 + 2 新测试);全套通过(115+2=117)。

- [ ] **Step 5: Commit(模块 C 一个)**

```bash
git add app/rag/store.py tests/test_rag_store.py
git commit -m "refactor(rag/store): Retriever Protocol + NumpyCosineRetriever + get_retriever

Move search() into a NumpyCosineRetriever behind a Retriever Protocol,
selected via get_retriever(); module-level search() becomes a shim. Keeps
cosine_top_k/_embedding_index/cache untouched. A FakeRetriever test proves
the backend is swappable (the documented sqlite-vec future).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task D:文档同步

**Files:**
- Modify: `docs/architecture.md`(关键决策 + 功能日志)
- Modify: `README.md`(结构树一行)

- [ ] **Step 1: `docs/architecture.md` 关键决策末尾加 #18**

在决策 17 之后追加:
```markdown
18. **数据源接口化(子项目 B)**:三处「可替换后端」——行情源、embedding、检索——各抽出一个 `typing.Protocol` 接口(`QuoteSource`/`Embedder`/`Retriever`),现有实现包成类(`TencentSource`/`SinaSource`/`EastmoneySource`、`FastembedEmbedder`、`NumpyCosineRetriever`)。三者干的事不同、**接口各自独立**,统一的只是「Protocol + 注册/选择」这套做法。行情源用注册表 `QUOTE_SOURCES`(替代 `_FETCHERS`);embedding/检索用 `get_embedder()`/`get_retriever()` 选择器。**纯重构、零新行为**:`fetch_quotes`/`embed_texts`/`store.search` 等被消费的模块级函数保留为兼容垫片,消费方一行不改。**不加配置开关**(YAGNI):embedding/检索各仅一个实现,选择器现在直接返回默认对象,留作将来第二实现的单一改动点(如检索换 sqlite-vec)。收益:加新后端不动老代码;测试用 fake 实现替代网络/模型(`FakeQuoteSource`/`FakeEmbedder`/`FakeRetriever`),failover/检索测试不再依赖外部。
```

- [ ] **Step 2: `docs/architecture.md` 功能日志末尾加一行**

```markdown
- **2026-06-01** 数据源接口化(子项目 B):行情源/embedding/检索三处各抽 `Protocol` 接口 + 实现类 + 注册/选择(`QuoteSource`/`Embedder`/`Retriever`);`_FETCHERS`→`QUOTE_SOURCES` 注册表,`get_embedder()`/`get_retriever()` 选择器;被消费函数保留为垫片,纯重构零新行为;新增 fake 实现解耦网络/模型测试。设计见 `docs/superpowers/specs/2026-06-01-stockbook-datasource-interfaces-design.md`,计划见 `docs/superpowers/plans/2026-06-01-datasource-interfaces.md`。
```

- [ ] **Step 3: `README.md` 结构树补一行(简略)**

把 `app/` 结构里 `quotes.py` 那行后,以及 rag 行的描述,点到接口化即可。具体:把 `quotes.py` 行改为:
```
  quotes.py        实时行情(QuoteSource 接口 + 多源注册表 + failover,解析与网络分离)
```
并在 `rag/` 行追加 `embed.py(Embedder 接口)`、`store.py(Retriever 接口)` 的括注(保持一行、简略)。

- [ ] **Step 4: 跑全套确认仍绿**

Run: `source .venv/bin/activate && pytest -q`
Expected: 全 117 通过。

- [ ] **Step 5: Commit(模块 D 一个)**

```bash
git add docs/architecture.md README.md
git commit -m "docs: record data-source interface refactor (subproject B)

architecture.md decision #18 + changelog; README structure note.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## 收尾

- `pytest -q` 全绿(117),消费方 `routers/`、`services.py` 未改 = 行为不变的证明。
- 用 `superpowers:finishing-a-development-branch` 决定整合方式。

## Self-Review 结果(写计划时已核对)

- **Spec 覆盖**:三个接口(Task A/B/C)、不拆包就近放(各模块内)、不加配置开关(选择器直接返回默认)、行为保留垫片(各 Task 的模块函数)、fake 测试(每 Task 都有)、文档(Task D)。全覆盖。
- **行为保留**:被消费名字清单逐一对照——`parse_*`/`to_*`/`is_trading_session`/`fetch_quotes`/`LAST_SOURCE`(A)、`embed_texts`/`embed_one`(B)、`cosine_top_k`/`search`/`sync_source`/`chunk_count`(C)均保留。`test_rag_ask.py` 的 `monkeypatch.setattr(embed,"embed_one",...)` 仍有效(embed_one 仍是模块级)。
- **既有测试兼容**:`test_quotes.py` 两个 failover 测试改用 `QUOTE_SOURCES`(原 `_FETCHERS` 已删);`test_rag_store.py` 的 `cosine_top_k` 测试不受影响。
- **命名一致**:`QuoteSource`/`QUOTE_SOURCES`、`Embedder`/`FastembedEmbedder`/`get_embedder`、`Retriever`/`NumpyCosineRetriever`/`get_retriever` 全程一致。
