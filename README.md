# 衡 · StockBook

个人、单用户、本地优先的投资策略追踪器。围绕「**大类资产配置 + 区间再平衡**」组织记账与追踪:
记账(标的层面买卖)→ 自下而上汇总到大类、对比目标 → 漂出容忍区间即给出加/减仓建议。

A 股优先(人民币计价),手动录入交易与现价,实时行情自动拉取。还内置一个 **Notion 知识库问答**(v2,见下),
让你对着自己的投资笔记提问。

技术栈:FastAPI + SQLAlchemy 2.0 + SQLite 单文件 + Jinja2 / 原生 JS,**零 Node 构建**,Python 3.9。
设计与架构详见 `docs/architecture.md`。

## 功能总览

**Phase 1 — 策略追踪(核心)**
- **仪表盘** — 目标配置(未分配池拨杆 + 大类增删改弹窗)、持仓与偏离(目标线 vs 实际圆点 + 容忍区间)、再平衡建议(回目标/回边缘、仅加仓、忽略零碎、再平衡提醒)。
- **持仓** — 每个标的的成本/现价/盈亏/预期卖价,展开看交易明细;按代码记交易自动建标的;卖出按买入批次配对(精确已实现盈亏)。
- **记录** — 买卖 + 资金注入/移出的总账,资金概览(总资产/净投入/总收益/已实现盈亏),现金可作为大类参与配置。
- **实时行情** — 按标的代码从腾讯/新浪/东方财富多源拉取(failover),自动或手动刷新。
- **备份/恢复** — SQLite 在线备份;重置/恢复前自动备份(可逆)。
- **只读分享** — `/?readonly=1` 隐藏所有写操作;追加 `&hideAmounts=1` 隐藏金额(只显百分比)。

**Phase 2 — Notion 知识库问答(RAG,默认关闭)**
- 浮动问答小窗:提问 → 检索你的 Notion 笔记 + 当前持仓快照 → Claude 回答(摘要 + 原文引用 + 可点回 Notion 的链接)。
- 本地零成本向量化(fastembed 中文 BGE)+ numpy 余弦检索,向量存在同一个 SQLite 文件里。
- 手动「同步」按钮:递归抓取指定 Notion 页面/数据库(含子页面),并发抓取 + 实时进度条。
- 成本/安全护栏:总开关、只读模式下禁用、每日调用上限;key 仅后端、不下发前端;同步阶段不调用 LLM。

## 运行

```bash
# 1. 准备环境
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. 起服务
uvicorn main:app --reload
```

打开 http://127.0.0.1:8000 。首次启动自动建库并预置一套示例策略(`stockbook.db`)。

启用 RAG 问答(可选)需要在项目根创建 `.env`(已被 `.gitignore` 忽略),启动时自动加载:

```bash
STOCKBOOK_RAG_ENABLED=1
NOTION_TOKEN=...            # Notion integration token,并把要纳入的页面分享给该 integration
ANTHROPIC_API_KEY=...       # Claude API key
```

然后在问答小窗里:添加 Notion 来源 → 点「同步」拉取并向量化 → 提问。

## 测试

```bash
pytest                       # 全套
pytest tests/test_calc.py    # 单个文件
```

测试用每例独立的临时 SQLite,绝不触碰你的 `stockbook.db`。

## 结构

```
main.py            FastAPI 入口(app 对象在此,便于 IDE/uvicorn 识别)
app/
  config.py        环境变量配置(DB / 只读 / 行情源 / RAG 开关与 key)
  database.py      SQLAlchemy engine / SessionLocal / get_db
  models.py        领域模型(Strategy/AssetClass/Security/Transaction/PriceQuote/CashFlow + RAG 两表)
  calc.py          纯计算引擎(持仓/占比/偏离/再平衡/未分配池)— 无框架依赖,易测
  services.py      ORM → 计算引擎 → 仪表盘 / 总账载荷
  quotes.py        实时行情(QuoteSource 接口 + 多源注册表 + failover,解析与网络分离)
  schemas.py       Pydantic 校验
  seed.py          建库 + 示例数据 + 轻量加列迁移 + 重置
  routers/         api.py(JSON API)· pages.py(页面)· rag.py(RAG 问答 API)
  rag/             notion.py(抓取/解析)· embed.py(向量化,Embedder 接口)· store.py(检索/入库,Retriever 接口)
                   · snapshot.py(持仓快照)· ask.py(prompt+Claude)· limiter.py(限流)
templates/         base.html(外壳)· index.html(单页 + 页内 tab)· _rag_widget.html(问答小窗)
static/            css/style.css · js/(common.js · app.js · rag.js)— 原生 JS,fetch 调 API
tests/             test_calc.py · test_api.py · test_quotes.py · test_rag_*.py
docs/architecture.md   技术选型、架构分层、关键决策、功能日志
```

## 环境变量

**核心(可选)**
- `STOCKBOOK_DATABASE_URL` — 默认 `sqlite:///<项目根>/stockbook.db`
- `STOCKBOOK_READONLY=1` — 整个实例强制只读
- `STOCKBOOK_HIDE_AMOUNTS=1` — 全局隐藏金额
- `STOCKBOOK_AUTO_REFRESH=0` — 关闭打开页面时自动拉取实时行情
- `STOCKBOOK_QUOTE_SOURCES` — 行情源 failover 顺序,默认 `tencent,sina,eastmoney`

**RAG 问答**
- `STOCKBOOK_RAG_ENABLED=1` — 启用问答功能(默认关)
- `NOTION_TOKEN` / `ANTHROPIC_API_KEY` — Notion 与 Claude 的密钥(走 `.env`)
- `STOCKBOOK_RAG_DAILY_LIMIT` — 每日问答上限,默认 `50`
- `STOCKBOOK_RAG_MODEL` — 回答模型,默认 Claude Haiku
