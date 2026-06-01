# 衡 · StockBook

个人、单用户、本地优先的投资策略追踪器。围绕「**大类资产配置 + 区间再平衡**」组织记账与追踪:
记账(标的层面买卖)→ 自下而上汇总到大类、对比目标 → 漂出容忍区间即给出加/减仓建议。

A 股优先(人民币计价),手动录入交易与现价。详见 `docs/superpowers/specs/2026-05-29-stockbook-strategy-tracker-design.md`。

## 运行

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

打开 http://127.0.0.1:8000 。首次启动会自动建库并预置一套示例策略(`stockbook.db`)。

单页 + 页内 tab:
- **仪表盘** — 目标配置(未分配池拨杆 + 大类增删改弹窗)、持仓与偏离、再平衡建议(回目标/回边缘、仅加仓、忽略零碎、再平衡提醒)。
- **持仓** — 每个标的的成本/现价/盈亏/预期卖价,展开看交易明细;记一笔交易时按代码自动建标的(新标的选大类,名称待联网补全)。
- 只读分享 `/?readonly=1` — 隐藏所有写操作、拨杆禁用;追加 `&hideAmounts=1` 隐藏金额。

实时行情:每次打开/刷新会按标的代码从腾讯行情(`qt.gtimg.cn`)自动拉取最新价(`source=auto`),
也可点右上角「↻ 刷新行情」手动更新。新标的名称会用行情里的名称自动补全。离线/打包给别人时
可用 `STOCKBOOK_AUTO_REFRESH=0` 关闭自动拉取(手动按钮仍可用)。

## 测试

```bash
pytest
```

## 结构

```
app/
  calc.py        纯计算引擎(持仓/占比/偏离/再平衡/未分配池)— 无框架依赖,易测
  models.py      SQLAlchemy 领域模型(策略感知、市场无关、价格可刷新)
  schemas.py     Pydantic 校验
  services.py    ORM → 计算引擎 → 仪表盘载荷
  seed.py        建库 + 示例数据 + 重置
  routers/       api.py(JSON API)· pages.py(Jinja2 页面)
templates/       dashboard.html · entry.html
static/          css/ · js/(原生 JS,fetch 调 API)
tests/           test_calc.py · test_api.py
```

## 环境变量(可选)

- `STOCKBOOK_DATABASE_URL` — 默认 `sqlite:///<项目根>/stockbook.db`
- `STOCKBOOK_READONLY=1` — 整个实例强制只读
- `STOCKBOOK_HIDE_AMOUNTS=1` — 全局隐藏金额
- `STOCKBOOK_AUTO_REFRESH=0` — 关闭打开页面时自动拉取实时行情
- `STOCKBOOK_QUOTE_SOURCES` — 行情源 failover 顺序,默认 `tencent,sina,eastmoney`(可调序或删减)
