# CLAUDE.md

「衡」StockBook —— 个人、单用户、本地优先的投资策略追踪器(大类资产配置 + 区间再平衡),A 股优先、人民币计价。FastAPI + SQLAlchemy 2.0 + SQLite 单文件 + Jinja2/原生 JS,**零 Node 构建**。Python 3.9。

完整背景见 @README.md(运行/环境变量)与 @docs/architecture.md(技术选型、架构分层、**关键决策**、功能日志)。开始任何改动前先读 architecture.md 的「关键决策」一节。

## 命令

```bash
source .venv/bin/activate          # 已有 venv,勿新建
pip install -r requirements-dev.txt # 测试/覆盖率工具(连带运行时依赖)
uvicorn main:app --reload          # 起服务(自动加载 .env)
pytest                             # 全套测试
pytest tests/test_calc.py -q       # 跑单个文件
pytest --cov=app                   # 全套 + 覆盖率报告
coverage report --include=app/calc.py,app/services.py --fail-under=95  # 核心覆盖率 gate(CI 同款)
```

## 硬性约定(YOU MUST)

- **绝不碰用户的 `stockbook.db`** —— 那是真实持仓数据。测试/本地起服务一律用临时库:`STOCKBOOK_DATABASE_URL=sqlite:////tmp/xxx.db`。`tests/` 的 `client` fixture 已自动隔离。
- **新增/改功能 = 同时更新 @docs/architecture.md**(关键决策 / API 一览 / 功能日志三节)与对应测试。这是本项目的铁律。
- **提交要模块化**:按连贯的功能切提交,不要每个微小步骤一个 commit(历史难回溯)。
- **绝不改写 git 历史 / 保留完整提交历史**:从项目起点起的完整提交历史须能反映真实开发进程(如何演进、问题如何被解决),不得压缩成少数大 commit。**禁用** `git commit --amend`、`git rebase`(含 `-i` 的 squash/fixup)、`git merge --squash`、`git push --force`。
- **先 review/验证、确认正确后再 commit**:让每个 commit 一次到位、无需返工(不靠 amend 修补)。若 commit 之后才发现要改,作为**独立的新跟进 commit** 追加——这恰好展示「问题如何被解决」,绝不回头改写已有 commit。
- **改动先开分支**,不直接在 `main` 上做实现;提交信息末尾附:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- **Python 3.9**:用 `typing.Optional/List/Dict`,不要 `X | None` 这类 3.10+ 写法。
- **密钥只走环境变量 / `.env`**(已 gitignore),绝不写进代码、`stockbook.db` 或下发前端。

## 架构要点(改代码前需知道的非显然之处)

- **持仓"推导而非存储"**:只存 `Transaction` 和 `PriceQuote`,持仓/成本/市值/占比/盈亏全由 `app/calc.py` 实时算。改了计算别去找"存储的余额"。
- **`calc.py` 是纯函数、无框架依赖**(输入输出皆 dataclass,易测);`services.py` 负责 ORM→calc 的转换。新计算逻辑放 `calc.py` 并配单测。
- **SQLite 加列迁移不上 Alembic**:新列在 `seed._migrate()` 里用 `ALTER TABLE` 补;新表靠 `create_all`。
- **RAG 问答(`app/rag/`)默认关闭**:需 `STOCKBOOK_RAG_ENABLED=1` + key。三重护栏(总开关 / 只读 403 / 每日限流)不可绕过。Notion 抓取/Claude 调用都在后端。
- **行情**:不在 `GET /api/dashboard` 内同步拉,前端「先渲染缓存价→静默刷新」。

## 验证

改完跑 `pytest` 看是否全绿(给我看输出,别只说"应该没问题")。涉及 UI/交互的改动,起服务实际点一下确认。
