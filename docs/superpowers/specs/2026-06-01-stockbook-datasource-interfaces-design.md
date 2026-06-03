# StockBook 子项目 B:数据源接口化 — 设计

> 状态:设计已确认,待写实现计划。
> 目标维度:**模块化 + 工程决策**。把三处「可替换的数据源后端」从「逻辑与实现缠在一起」重构成「**各自的接口 + 实现 + 注册表**」。
> **纯重构,不加任何新行为**:对外被消费的函数签名与运行结果完全不变,只理顺内部布线。

## 1. 背景

项目里有三类「同一件事可有多种做法、可互换」的后端,目前实现与「它是哪种后端」缠在一起写,加新实现要翻动一坨代码、易碰坏旁边的:

| 域 | 现状 | 为何该上接口 |
|---|---|---|
| **行情源** `quotes.py` | 非正式注册表 `_FETCHERS={name:fn}` + `fetch_quotes` 链式 failover;每源 `parse_*`(纯)+ `_fetch_*`(网络) | 已有腾讯/新浪/东方财富**三个干同一件事**的实现在互换,最名正言顺 |
| **向量化** `rag/embed.py` | 模块级 `_model` 单例,惰性加载 fastembed | 现仅一个实现;为「将来换 embedding」预留接缝 |
| **检索** `rag/store.py` | numpy 暴力余弦 `cosine_top_k` + 缓存 `_embedding_index` | 代码注释已写「日后上万片段可平滑换 sqlite-vec」 |

## 2. 核心约定:三个**独立**接口,**同一种**做法

**不是一个接口管三件事**——三者干的事不同,各有各的接口(各自的「合同」):

| 接口 | 规定「这一类后端必须能做的事」 |
|---|---|
| `QuoteSource` | 给一批 (代码, 市场) → 还 {代码: {价格, 名称}} |
| `Embedder` | 给一批文字 → 还一批向量 |
| `Retriever` | 给一个查询向量 → 还最相关的 top-k 片段 |

「统一」的只是**做法**(`Protocol` + 注册表 + 驱动函数这套套路),不是合同内容。接口只在「同类多个可互换实现」处才有意义。

## 3. 范围(YAGNI)

**做:**三处各抽出接口(`Protocol`)、把现有实现改造成实现类/对象、引入注册/选择,被消费的公共函数签名与行为**逐字不变**。补「用假实现替代网络/模型」的测试。

**不做:**
- 不加任何新功能、不改任何计算/抓取/检索结果。
- **不新增配置开关**:embedding/检索目前各只有一个实现,**不**加 `STOCKBOOK_EMBEDDER`/`STOCKBOOK_RETRIEVER` 之类 env(那才算新行为)。注册表 + 接口就是接缝,等真有第二个实现再加选择逻辑。行情源沿用现有 `STOCKBOOK_QUOTE_SOURCES`。
- 不引入插件入口点(entry-points)、不拆 `app/` 成多包、不动数据库 schema。
- 不碰 `calc.py`/`services.py` 的业务逻辑(`services.py` 仅用到 `quotes.is_trading_session`,签名不变即可)。

## 4. 关键决策(Decisions)

### D1. `typing.Protocol` 而非 `abc.ABC`
结构化(鸭子)接口:实现类**无需显式继承**接口,测试里塞个 fake 对象即可(只要方法签名对上)。ABC 的强制继承/注册在单用户本地项目里是过度仪式。Python 3.9 原生支持 `typing.Protocol`。

### D2. 不加新配置开关(YAGNI)
只有「同类 ≥2 个实现」时选择逻辑才有价值。当前仅行情源是多实现(沿用 `QUOTE_SOURCES`);embedding/检索各仅一个实现,**默认即唯一实现**,选择函数 `get_embedder()`/`get_retriever()` 现在直接返回默认对象,留作将来扩展的单一改动点。**接缝先立,开关后加。**

### D3. 接口/实现/驱动**就近放各自模块**,不新建 `app/interfaces.py`
「一起改的东西放一起」:`QuoteSource` 与三个源放 `quotes.py`,`Embedder` 放 `embed.py`,`Retriever` 放 `store.py`。避免一个「上帝接口文件」成为隐性耦合点。若某文件因此明显过大(如 `quotes.py` 现 231 行,预计 +50 行)再议拆分,但默认不拆。

### D4. 行为逐字保留 + 模块函数作为兼容垫片
所有当前被消费的公共函数(`fetch_quotes`、`LAST_SOURCE`、`embed_texts`、`embed_one`、`store.search`、`store.sync_source`、`store.chunk_count`、`cosine_top_k`、`is_trading_session`)**签名与语义不变**;内部改为委托给接口实现。消费方(`routers/api.py`、`routers/rag.py`、`services.py`)**一行不用改**。这是「重构」的硬标准:外部可观察行为零变化。

## 5. 三个接口的形态(概念,非最终代码)

**5.1 `QuoteSource`(`quotes.py`)**
- 接口:`name: str`;`fetch(items: List[Tuple[str, str]]) -> Dict[str, dict]`(传输失败抛 `httpx.HTTPError`,无可解析结果返回 `{}`)。
- 实现:`TencentSource`/`SinaSource`/`EastmoneySource`,各自封装原 `parse_*`(保持纯,仍单测)+ `_fetch_*`(网络)。
- 注册表:`QUOTE_SOURCES: Dict[str, QuoteSource]`(替代 `_FETCHERS`)。
- 驱动:`fetch_quotes(items, sources=None)` 签名/行为不变,遍历注册表按 `config.QUOTE_SOURCES` 顺序 failover,命中写 `LAST_SOURCE`。

**5.2 `Embedder`(`rag/embed.py`)**
- 接口:`embed_texts(texts: List[str]) -> List[List[float]]`;`embed_one(text: str) -> List[float]`。
- 实现:`FastembedEmbedder`(包住惰性 `_model` 加载与 `config.RAG_EMBED_MODEL`)。
- 选择:`get_embedder()` 返回默认 `FastembedEmbedder` 单例。模块级 `embed_texts`/`embed_one` 保留为垫片,委托给它。

**5.3 `Retriever`(`rag/store.py`)**
- 接口:`search(db, query_vec: List[float], k: Optional[int]) -> List[Dict]`。
- 实现:`NumpyCosineRetriever`(包住 `cosine_top_k` + `_embedding_index` 缓存)。
- 选择:`get_retriever()` 返回默认 `NumpyCosineRetriever`。模块级 `store.search` 保留为垫片,委托给它。
- 索引侧(`sync_source`/`replace_source_chunks`)留在 `store.py`,其内部对 embedding 的调用改走 `get_embedder()`(语义不变)。

## 6. 测试(接口化最实在的收益)

每个接口配一个 **fake 实现**,把原本依赖网络/模型的测试解耦:
- `FakeQuoteSource`(返回固定价表)→ 测 `fetch_quotes` 的 failover 链(某源抛错则跳下一个、先成功者用之、全挂则抛)**完全不联网**。
- `FakeEmbedder`(返回固定/确定性向量)→ 测 RAG 检索流程**不加载 fastembed 模型**。
- 复用现有 `cosine_top_k`/`store` 测试验证 `NumpyCosineRetriever` 行为与重构前一致。
- **回归基线**:重构前后跑全套 `pytest`,现有 111 测试保持全绿即证行为不变。

## 7. 文件改动一览
- 改:`app/quotes.py`(加 `QuoteSource` + 三实现类 + 注册表,`fetch_quotes` 改为驱动)。
- 改:`app/rag/embed.py`(加 `Embedder` + `FastembedEmbedder` + `get_embedder`)。
- 改:`app/rag/store.py`(加 `Retriever` + `NumpyCosineRetriever` + `get_retriever`,`search` 改委托)。
- 增:`tests/` 下 fake 实现 + failover/检索的接口测试(可并入现有 `test_quotes.py`/`test_rag_store.py` 或新建小文件)。
- 改:`docs/architecture.md`(关键决策 + 功能日志)、`README.md`(结构/简述,简略一行)。
- 消费方(`routers/api.py`、`routers/rag.py`、`services.py`):**不改**。

## 8. 风险 / 注意
- **「重构不改行为」的纪律**:每步用现有测试当安全网;任何测试因重构变红都必须查清是「重构引入的回归」还是「测试本就耦合实现细节」,前者必须修复到行为一致。
- **模块级单例与缓存**:`embed._model`、`store._embed_cache`、`quotes.LAST_SOURCE` 这些模块级状态在改造成对象后要保证**生命周期/缓存语义不变**(尤其 `_embed_cache` 的 `(count,max_id)` 失效策略、`LAST_SOURCE` 仍被 `api.py` 读取)。
- **`from __future__ import annotations`**:三个文件都有,`Protocol` 注解在 3.9 下安全(注解为字符串);但若有 `get_type_hints`/pydantic 求值路径需留意(本重构不涉及)。
- **绝不碰 `stockbook.db`**:测试用临时库;fake 实现让多数新测试连库都不需要。
