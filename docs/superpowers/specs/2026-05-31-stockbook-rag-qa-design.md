# StockBook Phase 2 设计文档 · Notion RAG 问答

- 日期:2026-05-31
- 状态:第一版 spec(待用户评审)
- 作者:chenchenmeng6 + Claude
- 依赖:Phase 1 追踪器(见 `2026-05-29-stockbook-strategy-tracker-design.md` 与 `docs/architecture.md`)

---

## 1. 定位

在现有追踪器上加一个**小问答窗口**:用户提问 → 检索个人 Notion 知识库 + 当前持仓快照 → Claude API 生成回答。

- **知识来源**:Notion 中**指定的几个页面/库**(策略/方法论、收藏的资料为主),约几百篇 page。
- **回答形式**:**摘要 + 原文引用 + 可点回 Notion 的链接**。
- **与追踪器的关系**:相对独立的子系统,平行新增,**不改动任何现有表/模块**。
- **设计原则延续 Phase 1**:单文件 SQLite、零 Node 构建、轻量可打包、纯函数易测。

---

## 2. 范围(Phase 2)

| 做 | 明确**不做**(留给后续) |
|---|---|
| Notion 指定页面/库拉取、解析、切块、本地向量化、入库 | 整个 workspace 全量抓取 |
| 手动「重新同步」按钮(删旧重建) | 自动定时同步 / 增量 diff |
| 本地 fastembed 向量化(中文 BGE) | API embedding |
| numpy 暴力余弦检索 top-k | 专业向量索引(sqlite-vec 等,留接口) |
| 摘要 + 原文引用 + Notion 链接 | 多轮对话记忆 / 会话历史 |
| 持仓快照**无脑附带**进 prompt | AI 按需 tool-calling 查持仓 |
| 浮动小窗 UI | 独立问答 tab |
| 后端限流 + 总开关 + 只读模式强制关 | 多用户 / 按 key 计费 |

---

## 3. 数据流

```
[同步阶段 · 手动触发,不调用 LLM]
Notion API 拉取指定页面/库
  → 解析 block 为纯文本 → 按标题/段落切块(chunk)
  → fastembed 本地向量化(中文 BGE)
  → 存入 SQLite:chunk 原文 + 向量 blob + Notion 来源链接
  (按 source 删旧片段后重建,不做增量 diff)

[问答阶段 · 每次提问]
用户问题 → fastembed 向量化
  → numpy 余弦相似度,取 top-k 片段
  → 取 App 当前持仓快照(精简版,复用 services 仪表盘数据)
  → 组装 prompt:[持仓快照] + [top-k 笔记片段] + [问题]
  → Claude API(默认 Haiku)回答(摘要 + 原文引用 + Notion 链接)
  → 浮动小窗展示
```

---

## 4. 数据模型(新增,平行于现有表)

延续「单文件 + 启动时轻量加列迁移」风格,**不动现有任何表**。

- **`NotionSource` 同步源**
  - `id`、`notion_id`(page/database 的 id)、`title`、`kind`(`page` / `database`)、`last_synced_at`
  - 即用户授权给 integration、希望纳入知识库的几个对象。

- **`KnowledgeChunk` 知识片段**
  - `id`、`source_id`(外键 → NotionSource)
  - `notion_page_id`、`notion_url`(可点回 Notion)
  - `title_path`(标题路径,如「策略 / 红利逻辑」)
  - `text`(片段原文)
  - `embedding`(向量,存为 blob)
  - `seq`(片段在原页内顺序)

- **重新同步语义**:按 `source` 删旧 `KnowledgeChunk` + 重新拉取重建(简单、无增量 diff)。

---

## 5. 检索实现:numpy 暴力余弦

- **决策**:几百篇 page → 切块后约**几千片段**。该规模下「全部向量读入内存 + numpy 一次性算余弦」是毫秒级,无需专业向量索引。
- **不用 sqlite-vec 的理由**:它需在 macOS 加载 C 扩展,系统自带 Python 常禁用扩展加载(已知坑);为当前规模引入此风险不值得。项目保持零运维、单文件。
- **向量存取**:`KnowledgeChunk.embedding` 存 blob;查询时取出全部、numpy 算余弦、排序取 top-k。
- **预留扩展**:检索逻辑封装在 `store.py` 的一个函数后,日后上万片段再换 sqlite-vec 不影响调用方。

---

## 6. 笔记 + 持仓结合(v1 克制方案)

- **做法**:每次提问时,把一份**精简持仓快照**(各大类目标/当前占比、主要标的、盈亏概要——复用 `services.py` 现成的仪表盘数据)一并塞进 prompt。
- **效果**:可回答「我对 XX 的看法 + 现在持仓如何」这类混合问题。
- **明确不做**:不做 AI 自主 tool-calling 查持仓(更大工程,留后续)。就无脑附带精简快照,token 成本低。

---

## 7. 安全与成本(本功能的重点)

### 7.1 风险 A:密钥泄露

- **只从环境变量读** `NOTION_TOKEN` / `ANTHROPIC_API_KEY`,绝不写进代码或 `stockbook.db`;走 `.env`(确认 `.env` 在 `.gitignore`)。
- **key 永不下发前端**:所有 Claude / Notion 调用在后端;前端只调 `/api/rag/ask`,只拿回答文本。
- **打包分享时** `.env` 不进包,别人需填自己的 key,用户 key 不外流。
- **只读分享强制关**:`readonly=1` 时 `/api/rag/*` 后端直接返回 403(不只是前端藏窗),从根杜绝「别人用你的 key」。

### 7.2 风险 B:花费失控

- **总开关** `STOCKBOOK_RAG_ENABLED`:未显式开启则整功能下线,RAG 路由不挂载。
- **后端限流**:`/api/rag/ask` 每日调用上限,默认 **50 次/天**,经环境变量 `STOCKBOOK_RAG_DAILY_LIMIT` 可改;超限返回友好提示,不再打 Claude。
- **上下文裁剪**:限定 k 值 + 每片段截断 + 持仓快照精简,控制每请求 input token。
- **embedding 本地零成本**:向量化不花钱;**同步阶段不调 LLM**(只 Notion + 本地 embed),重建索引不烧 LLM 钱。
- **模型档位**:默认 **Haiku**(便宜快、问答够用),经环境变量 `STOCKBOOK_RAG_MODEL` 可切 Opus 等。

### 7.3 新增环境变量一览

| 变量 | 默认 | 说明 |
|---|---|---|
| `STOCKBOOK_RAG_ENABLED` | 关 | RAG 功能总开关 |
| `NOTION_TOKEN` | — | Notion integration token(App 自己的,非会话里的) |
| `ANTHROPIC_API_KEY` | — | Claude API key |
| `STOCKBOOK_RAG_DAILY_LIMIT` | `50` | 每日问答上限 |
| `STOCKBOOK_RAG_MODEL` | Haiku | 回答模型 |

---

## 8. 界面

- **浮动小窗**:右下角可开合的聊天气泡,任何 tab 下都能提问。
- 回答区:摘要在上,下面列引用的原文片段 + 可点回 Notion 的链接。
- 同步入口:一个「重新同步知识库」按钮(展示上次同步时间、片段数)。
- **只读分享模式**:隐藏浮动小窗(且后端 403 兜底)。

---

## 9. 架构与模块布局

贴合现有 `app/` 分层,新增 `app/rag/` 子包:

| 模块 | 职责 |
|---|---|
| `app/rag/notion.py` | Notion 拉取 + block→纯文本解析 + 切块(网络与解析分离,便于测试) |
| `app/rag/embed.py` | fastembed 本地向量化(中文 BGE) |
| `app/rag/store.py` | KnowledgeChunk 存取 + numpy 余弦检索(检索接口封装在此) |
| `app/rag/ask.py` | 组装 prompt(持仓快照 + top-k 片段) + 调 Claude |
| `routers/api.py`(扩展) | `POST /api/rag/sync`、`POST /api/rag/ask`、`GET /api/rag/status`、源管理 |
| `config.py`(扩展) | 上述环境变量 |
| `models.py`(扩展) | `NotionSource`、`KnowledgeChunk` |

### 新依赖

- `fastembed`(ONNX,几十 MB)、`anthropic`(Claude SDK)、`numpy`、`notion-client`。
- 比 torch 系轻,基本不破坏「可打包」优点。

---

## 10. JSON API(新增)

- `GET /api/rag/status` — 是否启用、源列表、上次同步时间、片段数、今日剩余配额。
- `POST /api/rag/sync` — 手动重新同步(删旧重建);返回各源片段数。
- `POST /api/rag/ask` — 提问;受总开关 + 限流 + readonly 403 三重约束;返回摘要 + 引用片段(含 notion_url)。
- 源管理(可选,v1 可先用配置/手动):`POST/DELETE /api/rag/sources`。

---

## 11. 错误处理与兜底

- **未配置 key / 未开启**:`/api/rag/*` 返回明确提示(而非 500)。
- **Notion 拉取失败**:同步报告哪些源失败,不影响已成功部分。
- **检索为空**:无相关片段时如实告知「知识库里没找到相关内容」,不让 Claude 凭空编。
- **限流命中**:返回「今日问答已达上限」友好文案。
- **readonly**:后端 403,前端不显示窗口。

---

## 12. 测试策略

- `notion.py`:block→文本、切块逻辑(喂样例 block JSON,纯解析,不打网络)。
- `store.py`:向量入库/取出、numpy 余弦排序 top-k 正确性。
- `ask.py`:prompt 组装(持仓快照 + 片段拼接);Claude 调用 mock。
- API:总开关关闭时 404/提示、readonly 时 403、限流计数。
- 沿用 Phase 1 约定:测试用临时库,绝不动 `stockbook.db`。

---

## 13. 已确认的关键决策(对话纪要)

1. Phase 2 独立子系统,平行新增,不改现有表/模块。
2. Notion 内容:策略/方法论 + 收藏的资料,约几百篇 page。
3. 抓取范围:指定几个页面/库(非整个 workspace)。
4. 回答形式:摘要 + 原文引用 + Notion 链接。
5. 笔记 + 持仓结合,但**保持简单**:无脑附带精简持仓快照,不用 tool-calling。
6. 同步:手动「重新同步」按钮,删旧重建,无增量。
7. UI:浮动小窗;只读分享模式关掉问答(后端 403 兜底)。
8. 向量化:本地零成本 fastembed(中文 BGE);检索 numpy 暴力余弦(非 sqlite-vec)。
9. 安全:key 只在后端、走 .env、不下发前端、打包不带 key。
10. 成本:总开关 + 每日限流(默认 50,环境变量可改) + 上下文裁剪 + 默认 Haiku(可切)。

---

## 14. 未决问题 / 后续

1. 自动/定时同步、增量 diff(v1 手动重建)。
2. 多轮对话记忆 / 会话历史(v1 单轮)。
3. AI 自主 tool-calling 查持仓/行情(v1 无脑附带快照)。
4. 上万片段后换 sqlite-vec 或专业向量库(接口已封装)。
5. 源管理 UI 的完整形态(v1 可先配置/简单接口)。
6. 引用片段的高亮/定位到 Notion 块级锚点。
