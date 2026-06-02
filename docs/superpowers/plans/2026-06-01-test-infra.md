# 测试基建(子项目 E)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 StockBook 加自动化测试安全网:GitHub Actions CI 跑全套 pytest + Hypothesis 验 `calc.py` 核心不变量 + 核心模块覆盖率硬 gate。

**Architecture:** 纯增测试与基建,**不改任何业务代码**。新增 `requirements-dev.txt`(开发期工具,不污染运行时依赖)、`pyproject.toml`(coverage/pytest 工具配置)、`tests/test_calc_properties.py`(property-based 不变量)、`.github/workflows/ci.yml`(CI)。覆盖率分层:`pytest --cov=app` 出全量报告,但只对 `calc.py`+`services.py` 用 `coverage report --include=... --fail-under` 设硬失败线。

**Tech Stack:** Python 3.9、pytest 8.4、pytest-cov、coverage、Hypothesis、GitHub Actions。

**Spec:** `docs/superpowers/specs/2026-06-01-stockbook-test-infra-design.md`

**前置约定(贯穿全程):**
- 已在分支 `feat/test-infra` 上;每个 commit 末尾附 `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`。
- 跑测试用项目自带 venv:`source .venv/bin/activate`。**绝不碰 `stockbook.db`**(本子项目全是纯函数/临时库,天然安全)。
- Python 3.9:类型标注用 `typing.Optional/List`,不用 `X | None`。

---

## 关于 property 测试的 TDD 节奏(重要)

`calc.py` 已存在且正确,所以 property 测试**不会**走「先红后绿」的常规 TDD。正确的纪律是:
1. 写 property 测试 → 跑,**期望 PASS**(对正确代码成立);
2. **变异检查(mutation check)**:临时改坏 `calc.py` 一处,跑同一测试,**确认它 FAIL**(证明测试有牙、不是永真);
3. `git checkout app/calc.py` 还原变异 → 再跑确认 PASS → commit。

每个 property 任务都包含这个变异检查步骤。**变异只动 `app/calc.py` 且必须还原,绝不提交变异。**

---

## Task 1: dev 依赖 + 覆盖率基线测量

**Files:**
- Create: `requirements-dev.txt`

- [ ] **Step 1: 创建 `requirements-dev.txt`**

```
# 开发期工具(测试 + 覆盖率)。运行时依赖见 requirements.txt,本文件不污染它。
-r requirements.txt
pytest-cov==5.0.0
coverage==7.6.1
hypothesis==6.112.2
```

- [ ] **Step 2: 安装并验证(若某 pin 在 3.9 上解析失败,降到最近的兼容版本)**

Run:
```bash
source .venv/bin/activate && pip install -r requirements-dev.txt
```
Expected: 安装成功;`pytest --version`、`coverage --version`、`python -c "import hypothesis; print(hypothesis.__version__)"` 都能跑。

- [ ] **Step 3: 测量当前覆盖率基线(只看,不卡)**

Run:
```bash
source .venv/bin/activate && pytest --cov=app --cov-report=term-missing -q
```
然后单独看核心两文件的合并数:
```bash
coverage report --include=app/calc.py,app/services.py
```
Expected: 全套测试 PASS;**记下 `app/calc.py` + `app/services.py` 合并的 TOTAL 覆盖率百分比**(Task 7 据此定 `--fail-under`)。若合并 < 95%,Task 7 会补针对性测试或据实定阈值。

- [ ] **Step 4: Commit**

```bash
git add requirements-dev.txt
git commit -m "build: add requirements-dev.txt (pytest-cov, coverage, hypothesis)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: pyproject.toml 工具配置

**Files:**
- Create: `pyproject.toml`

- [ ] **Step 1: 创建 `pyproject.toml`(仅工具配置,不含 `[build-system]`/`[project]`,避免被当成可装包)**

```toml
# 仅放工具配置 —— StockBook 不是可 pip 安装的包,故意不写 [build-system]/[project]。
[tool.coverage.run]
source = ["app"]
# 入口胶水无独立逻辑、由 import 即覆盖,排除以免噪音:
omit = ["app/main.py"]

[tool.coverage.report]
# 让 `# pragma: no cover` 与防御性分支不虚降数字;具体行内标注按需加。
exclude_lines = ["pragma: no cover", "raise NotImplementedError"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
```

- [ ] **Step 2: 验证配置生效(testpaths 收敛、覆盖率 source 生效)**

Run:
```bash
source .venv/bin/activate && pytest --cov=app -q
```
Expected: 测试照常全绿,覆盖率报告只统计 `app/`(不含 `tests/`)。

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "build: add pyproject.toml for coverage/pytest config

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: property 测试脚手架 + 原始函数不变量(I4, I5)

**Files:**
- Create: `tests/test_calc_properties.py`

- [ ] **Step 1: 写测试文件(共享策略 + I4/I5)**

```python
"""Property-based 不变量测试(Hypothesis)for the pure calc engine.

calc.py 已正确;这些测试断言「对任意合法输入,不变量恒成立」,
覆盖 example-based 测试漏掉的边界(零总资产、负现金、空仓、单类等)。
变异检查见 plan:每条不变量都验证过「改坏 calc 能被抓到」。
"""
import math
from dataclasses import dataclass
from typing import Optional

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from app.calc import (
    AssetClassInput,
    SecurityInput,
    average_cost,
    compute_dashboard,
    net_shares,
    open_lots,
    rebalance_amount,
)


# --------------------------------------------------------------------------- #
# 共享策略:生成有限、合理量级的浮点(避免极值让浮点比较失去意义)。
# --------------------------------------------------------------------------- #
finite = dict(allow_nan=False, allow_infinity=False)
prices = st.floats(min_value=0.01, max_value=1e6, **finite)
shares = st.floats(min_value=0.0, max_value=1e6, **finite)
weights = st.floats(min_value=0.0, max_value=100.0, **finite)


@dataclass
class Tx:
    """最小交易对象,够 net_shares / open_lots / average_cost 用。"""
    id: int
    action: str           # "buy" | "sell"
    shares: float
    price: float = 0.0
    matched_buy_id: Optional[int] = None


@st.composite
def matched_txs(draw):
    """一组买入 + 全部已配对到某买入批次的卖出。"""
    n_buys = draw(st.integers(min_value=1, max_value=5))
    buys = [
        Tx(id=i, action="buy", shares=draw(shares), price=draw(prices))
        for i in range(1, n_buys + 1)
    ]
    n_sells = draw(st.integers(min_value=0, max_value=5))
    sells = []
    for k in range(n_sells):
        target = draw(st.sampled_from(buys))
        # 卖出股数不超过该批次原股数(贴近真实约束;identity 本身不要求)。
        sells.append(Tx(id=1000 + k, action="sell",
                        shares=draw(st.floats(min_value=0.0, max_value=target.shares, **finite)),
                        matched_buy_id=target.id))
    return buys + sells


# --------------------------------------------------------------------------- #
# I4: net_shares == Σ 未平仓批次剩余(所有卖出都已配对时)
# --------------------------------------------------------------------------- #
@given(txs=matched_txs())
def test_net_shares_equals_sum_of_open_lot_remaining(txs):
    net = net_shares(txs)
    remaining = sum(rem for _buy, rem in open_lots(txs))
    assert math.isclose(net, remaining, abs_tol=1e-6)


# --------------------------------------------------------------------------- #
# I5: average_cost 落在 [最低买价, 最高买价](有未平仓批次时)
# --------------------------------------------------------------------------- #
@given(txs=matched_txs())
def test_average_cost_within_buy_price_range(txs):
    avg = average_cost(txs)
    open_prices = [buy.price for buy, rem in open_lots(txs) if rem > 0]
    assume(open_prices)               # 无未平仓批次时 avg 为 None,跳过
    assert avg is not None
    assert min(open_prices) - 1e-6 <= avg <= max(open_prices) + 1e-6
```

- [ ] **Step 2: 跑,期望 PASS**

Run:
```bash
source .venv/bin/activate && pytest tests/test_calc_properties.py -q
```
Expected: PASS(2 个 property,各跑约 100 例)。

- [ ] **Step 3: 变异检查 I4**

临时改 `app/calc.py` 的 `net_shares`:把 `total += shares if action == "buy" else -shares` 改成 `total += shares`(永远加)。
Run: `pytest tests/test_calc_properties.py::test_net_shares_equals_sum_of_open_lot_remaining -q`
Expected: **FAIL**(Hypothesis 给出反例)。然后还原:
```bash
git checkout app/calc.py
```

- [ ] **Step 4: 变异检查 I5**

临时改 `average_cost`:把 `num += remaining * buy.price` 改成 `num += remaining * buy.price * 2`。
Run: `pytest tests/test_calc_properties.py::test_average_cost_within_buy_price_range -q`
Expected: **FAIL**。还原:`git checkout app/calc.py`,再跑 `pytest tests/test_calc_properties.py -q` 确认全 PASS。

- [ ] **Step 5: Commit**

```bash
git add tests/test_calc_properties.py
git commit -m "test: property invariants for net_shares/open_lots/average_cost (I4,I5)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: 再平衡不变量(I3, I6)

**Files:**
- Modify: `tests/test_calc_properties.py`(追加)

- [ ] **Step 1: 追加 I6(rebalance_amount 符号)**

在文件末尾追加:
```python
# --------------------------------------------------------------------------- #
# I6: rebalance_amount 符号 —— 欠配(current<target)为正、超配为负、相等为 0
# --------------------------------------------------------------------------- #
@given(
    target=weights,
    current=weights,
    total=st.floats(min_value=0.0, max_value=1e9, **finite),
)
def test_rebalance_amount_sign(target, current, total):
    amount = rebalance_amount(target, current, total)
    assume(total > 0)                 # total==0 时金额恒为 0,无方向可言
    if target > current:
        assert amount > -1e-6         # 欠配 → 买入(非负)
    elif target < current:
        assert amount < 1e-6          # 超配 → 卖出(非正)
    else:
        assert math.isclose(amount, 0.0, abs_tol=1e-6)
```

- [ ] **Step 2: 追加 I3(对每类施加 rebalance_amount 后落到 target)**

继续追加:
```python
# --------------------------------------------------------------------------- #
# I3: 对非负大类施加其 rebalance_amount 后,新占比落到 target_weight
# --------------------------------------------------------------------------- #
@st.composite
def nonneg_dashboards(draw):
    """1–5 个非负大类(证券价/股数皆 ≥0 → class_mv ≥0),无现金类。"""
    n = draw(st.integers(min_value=1, max_value=5))
    classes = []
    for i in range(n):
        secs = draw(st.lists(
            st.builds(SecurityInput,
                      id=st.integers(min_value=1, max_value=10_000),
                      code=st.just("X"), name=st.just("X"), market=st.just("CN"),
                      shares=shares, price=prices,
                      avg_cost=st.one_of(st.none(), prices)),
            max_size=4))
        classes.append(AssetClassInput(
            id=i + 1, name="C%d" % i, target_weight=draw(weights),
            band_low=draw(weights), band_high=draw(weights),
            color="#000000", sort_order=i, is_cash=False, securities=secs))
    return classes


@settings(max_examples=200, deadline=None)
@given(classes=nonneg_dashboards())
def test_rebalance_lands_on_target(classes):
    dash = compute_dashboard(classes)
    denom = sum(max(0.0, ac.market_value) for ac in dash.asset_classes)
    assume(denom > 0)
    for ac in dash.asset_classes:
        assert ac.rebalance_amount is not None
        new_value = ac.market_value + ac.rebalance_amount
        new_weight = new_value / denom * 100.0
        assert math.isclose(new_weight, ac.target_weight, abs_tol=1e-4)
```

- [ ] **Step 3: 跑,期望 PASS**

Run: `pytest tests/test_calc_properties.py -q`
Expected: PASS(现共 4 个 property)。

- [ ] **Step 4: 变异检查 I3**

临时改 `compute_dashboard` 里 `reb_amount` 的算式 `rebalance_amount(ac.target_weight, current_weight, weight_denom)` → 把 `ac.target_weight` 写成 `ac.target_weight + 1`。
Run: `pytest tests/test_calc_properties.py::test_rebalance_lands_on_target -q`
Expected: **FAIL**。还原 `git checkout app/calc.py`。

- [ ] **Step 5: 变异检查 I6**

临时改 `rebalance_amount`:`return (target_weight - current_weight) / 100.0 * total_assets` → `return (current_weight - target_weight) / 100.0 * total_assets`(符号反)。
Run: `pytest tests/test_calc_properties.py::test_rebalance_amount_sign -q`
Expected: **FAIL**。还原 `git checkout app/calc.py`,再跑全文件确认 PASS。

- [ ] **Step 6: Commit**

```bash
git add tests/test_calc_properties.py
git commit -m "test: property invariants for rebalance amount/direction (I3,I6)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: 仪表盘占比不变量(I1, I2, I7)

**Files:**
- Modify: `tests/test_calc_properties.py`(追加)

- [ ] **Step 1: 追加生成「含现金、可负余额」的完整仪表盘策略 + I1/I2/I7**

```python
# --------------------------------------------------------------------------- #
# 含现金类与任意现金余额(可负)的完整仪表盘
# --------------------------------------------------------------------------- #
@st.composite
def full_dashboards(draw):
    classes = draw(nonneg_dashboards())
    cash_balance = 0.0
    if draw(st.booleans()):
        # 末尾追加一个现金类;余额可正可负(测负现金 floor 行为)。
        classes.append(AssetClassInput(
            id=len(classes) + 1, name="Cash", target_weight=draw(weights),
            band_low=draw(weights), band_high=draw(weights),
            color="#000000", sort_order=len(classes), is_cash=True, securities=[]))
        cash_balance = draw(st.floats(min_value=-1e6, max_value=1e6, **finite))
    return classes, cash_balance


# I1: 各大类 current_weight 之和 ≈ 100%(weight_denom>0 时)。
#     注:这是「实际占比」(由市值推导、构造上恒等 100%),
#     与带未分配池的「目标占比」(I8)是不同的量。
@settings(max_examples=200, deadline=None)
@given(data=full_dashboards())
def test_current_weights_sum_to_100(data):
    classes, cash = data
    dash = compute_dashboard(classes, cash_balance=cash)
    denom = sum(max(0.0, ac.market_value) for ac in dash.asset_classes)
    assume(denom > 0)
    total = sum(ac.current_weight for ac in dash.asset_classes
                if ac.current_weight is not None)
    assert math.isclose(total, 100.0, abs_tol=1e-4)


# I2: 每个 current_weight 恒在 [0, 100](含负现金 floor 到 0)。
@settings(max_examples=200, deadline=None)
@given(data=full_dashboards())
def test_current_weight_bounded_0_100(data):
    classes, cash = data
    dash = compute_dashboard(classes, cash_balance=cash)
    for ac in dash.asset_classes:
        if ac.current_weight is not None:
            assert -1e-6 <= ac.current_weight <= 100.0 + 1e-6


# I7: 对任意合法输入不抛异常,且输出无 NaN/inf。
@settings(max_examples=300, deadline=None)
@given(data=full_dashboards())
def test_compute_dashboard_robust(data):
    classes, cash = data
    dash = compute_dashboard(classes, cash_balance=cash)
    for ac in dash.asset_classes:
        for v in (ac.market_value, ac.current_weight, ac.deviation,
                  ac.rebalance_amount):
            assert v is None or math.isfinite(v)
        for sv in ac.securities:
            for v in (sv.market_value, sv.cost_value, sv.unrealized_pnl,
                      sv.pnl_pct, sv.weight_in_class, sv.weight_in_total):
                assert v is None or math.isfinite(v)
```

- [ ] **Step 2: 跑,期望 PASS**

Run: `pytest tests/test_calc_properties.py -q`
Expected: PASS(现共 7 个 property)。

- [ ] **Step 3: 变异检查 I1**

临时改 `compute_dashboard` 的 `weight_denom = sum(max(0.0, v) for v in class_values.values())` → 去掉 floor:`weight_denom = sum(class_values.values())`。
Run: `pytest tests/test_calc_properties.py::test_current_weights_sum_to_100 tests/test_calc_properties.py::test_current_weight_bounded_0_100 -q`
Expected: **FAIL**(负现金时占比和/上界被破坏)。还原 `git checkout app/calc.py`。

- [ ] **Step 4: 还原后全跑**

Run: `pytest tests/test_calc_properties.py -q`
Expected: 全 7 个 PASS。

- [ ] **Step 5: Commit**

```bash
git add tests/test_calc_properties.py
git commit -m "test: property invariants for dashboard weights & robustness (I1,I2,I7)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: 目标占比 / 未分配不变量(I8)

**Files:**
- Modify: `tests/test_calc_properties.py`(追加)

- [ ] **Step 1: 追加 I8**

```python
# --------------------------------------------------------------------------- #
# I8: 目标占比规则 —— unallocated == 100 − Σtarget;is_balanced ⟺ |unallocated|<eps。
#     管的是「目标占比」(拨杆,带未分配池,不一定 100%),与 I1 是不同的量。
# --------------------------------------------------------------------------- #
@given(targets=st.lists(weights, min_size=1, max_size=6))
def test_unallocated_and_is_balanced(targets):
    classes = [
        AssetClassInput(id=i + 1, name="C%d" % i, target_weight=t,
                        band_low=0.0, band_high=100.0, color="#000000",
                        sort_order=i, is_cash=False, securities=[])
        for i, t in enumerate(targets)
    ]
    eps = 1e-6
    dash = compute_dashboard(classes, epsilon=eps)
    assert math.isclose(dash.unallocated, 100.0 - sum(targets), abs_tol=1e-9)
    assert dash.is_balanced == (abs(dash.unallocated) < eps)


# 目标恰好凑成 100 时,is_balanced 必为 True(未分配 = 0)。
@given(
    head=st.lists(st.floats(min_value=0.0, max_value=100.0, **finite),
                  min_size=0, max_size=4),
)
def test_targets_summing_to_100_are_balanced(head):
    assume(sum(head) <= 100.0)
    targets = head + [100.0 - sum(head)]      # 最后一类补足到 100
    classes = [
        AssetClassInput(id=i + 1, name="C%d" % i, target_weight=t,
                        band_low=0.0, band_high=100.0, color="#000000",
                        sort_order=i, is_cash=False, securities=[])
        for i, t in enumerate(targets)
    ]
    dash = compute_dashboard(classes, epsilon=1e-6)
    assert dash.is_balanced
```

- [ ] **Step 2: 跑,期望 PASS**

Run: `pytest tests/test_calc_properties.py -q`
Expected: PASS(现共 9 个 property)。

- [ ] **Step 3: 变异检查 I8**

临时改 `compute_dashboard`:`unallocated = 100.0 - sum(...)` → `unallocated = 90.0 - sum(...)`。
Run: `pytest tests/test_calc_properties.py::test_unallocated_and_is_balanced -q`
Expected: **FAIL**。还原 `git checkout app/calc.py`,再跑全文件确认 9 个 PASS。

- [ ] **Step 4: Commit**

```bash
git add tests/test_calc_properties.py
git commit -m "test: property invariants for target weights/unallocated (I8)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: 覆盖率 gate(核心模块硬失败线)

**Files:**
- 无新文件;确定 `--fail-under` 阈值并验证 gate 行为。

- [ ] **Step 1: 测量加了 property 测试后的核心覆盖率**

Run:
```bash
source .venv/bin/activate && pytest --cov=app -q
coverage report --include=app/calc.py,app/services.py
```
Expected: 全套 PASS;**记下合并 TOTAL %**。

- [ ] **Step 2: 确定阈值 N**

- 若合并 TOTAL ≥ 95 → 取 `N = 95`。
- 若 < 95 → 先 `coverage report --include=app/calc.py,app/services.py --show-missing` 看未覆盖行;
  在 `tests/test_calc.py`(calc 漏的)或 `tests/test_api.py`(services 漏的)补**针对性 example 测试**覆盖那些行,重测直到 ≥ 95;仍补不上的纯防御分支(理论不可达)在 `calc.py`/`services.py` 行尾加 `# pragma: no cover` 并在 commit message 说明。目标:`N = 95`。

- [ ] **Step 3: 验证 gate 命令(真能卡)**

先用一个**高于实际**的阈值确认会失败:
```bash
coverage report --include=app/calc.py,app/services.py --fail-under=100
```
Expected(除非真是 100%): 退出码非 0(`echo $?` 看到非 0),证明 gate 有效。
再用最终阈值确认通过:
```bash
coverage report --include=app/calc.py,app/services.py --fail-under=95 ; echo "exit=$?"
```
Expected: `exit=0`。

- [ ] **Step 4: 如有补测,提交**

```bash
git add -A
git commit -m "test: lift core coverage to >=95% for the gate

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```
(若无需补测则跳过本步。)

---

## Task 8: GitHub Actions CI

**Files:**
- Create: `.github/workflows/ci.yml`

- [ ] **Step 1: 创建 workflow**

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python 3.9
        uses: actions/setup-python@v5
        with:
          python-version: "3.9"
          cache: pip
          cache-dependency-path: requirements-dev.txt

      - name: Install dependencies
        run: pip install -r requirements-dev.txt

      - name: Run tests with coverage
        run: pytest --cov=app --cov-report=term-missing

      - name: Coverage report (full, informational)
        run: coverage report

      - name: Core coverage gate (calc.py + services.py)
        run: coverage report --include=app/calc.py,app/services.py --fail-under=95
```
> 注:`pytest` 步骤生成 `.coverage`,后两步复用它不重跑。`--fail-under` 用 Task 7 定的 N(此处 95)。

- [ ] **Step 2: 本地用 act 或人工核对(无法本地跑则跳到推送验证)**

逐条核对:`requirements-dev.txt` 含 `-r requirements.txt`(连带运行时);最后一步阈值与 Task 7 一致。

- [ ] **Step 3: Commit + 推送,在 GitHub 看 Actions 跑绿**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: GitHub Actions running pytest + layered coverage gate

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
git push -u origin feat/test-infra
```
Expected: 在 GitHub Actions 页看到 workflow **绿**。若红,据日志修(常见:pin 版本在 CI 解析失败 → 调 Task 1 的 pin)。

---

## Task 9: 文档同步(项目铁律)

**Files:**
- Modify: `docs/architecture.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: `docs/architecture.md` 加关键决策**

在「4. 关键决策」末尾追加:
```markdown
17. **测试基建(子项目 E)**:CI(GitHub Actions,Python 3.9 单版本)跑全套 pytest;Hypothesis 对纯函数 `calc.py` 验核心不变量(实际占比和≈100%、占比∈[0,100]、再平衡后落目标、`net_shares`=未平仓批次剩余之和、均价∈[买价区间]、再平衡金额符号、任意输入不崩、目标未分配=100−Σtarget)。覆盖率**分层 gate**:`pytest --cov=app` 出全量报告,但只对 `calc.py`+`services.py` 用 `coverage report --include=... --fail-under=95` 设硬失败线(外围网络/模型代码只报告不卡)。dev 工具独立于 `requirements-dev.txt`,不污染运行时依赖。**不加 lint**:3.9 语法守卫无可靠静态工具(ruff `UP` 是旧→新升级、不报 `X|None`),且本地+CI 跑 3.9 已是天然闸。
```

- [ ] **Step 2: `docs/architecture.md` 加功能日志**

在「7. 功能日志」末尾追加:
```markdown
- **2026-06-01** 测试基建(子项目 E):GitHub Actions CI(pytest + 覆盖率)+ Hypothesis 核心不变量套件(`tests/test_calc_properties.py`,I1–I8)+ 覆盖率分层 gate(核心 `calc.py`/`services.py` `--fail-under=95`)+ `requirements-dev.txt`/`pyproject.toml`。评估后排除 lint(3.9 守卫无可靠工具)。设计见 `docs/superpowers/specs/2026-06-01-stockbook-test-infra-design.md`,计划见 `docs/superpowers/plans/2026-06-01-test-infra.md`。
```

- [ ] **Step 3: `CLAUDE.md` 命令区补充**

把「命令」代码块更新为(在 `pytest` 行附近加):
```bash
pip install -r requirements-dev.txt   # 装测试/覆盖率工具(连带运行时依赖)
pytest --cov=app                       # 全套测试 + 覆盖率
coverage report --include=app/calc.py,app/services.py --fail-under=95  # 核心 gate
```

- [ ] **Step 4: 跑全套确认仍绿**

Run: `source .venv/bin/activate && pytest -q`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add docs/architecture.md CLAUDE.md
git commit -m "docs: record test-infra (subproject E) decisions + changelog + commands

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## 收尾

全部任务完成后:
- `pytest -q` 全绿、`coverage report --include=app/calc.py,app/services.py --fail-under=95` 退出码 0。
- GitHub Actions 在 PR/main 上跑绿。
- 用 `superpowers:finishing-a-development-branch` 决定合并方式(PR / 直接合并)。

## Self-Review 结果(写计划时已核对)

- **Spec 覆盖**:CI(Task 8)、Hypothesis I1–I8(Task 3–6)、覆盖率分层 gate(Task 7)、`requirements-dev.txt`(Task 1)、`pyproject.toml`(Task 2)、文档同步(Task 9)、排除 lint(spec §1/D5,Task 9 决策已记)。全覆盖。
- **I8 落点**:已确认 `Dashboard.unallocated`/`is_balanced` 即纯函数,**无需重构**(spec §7 风险消解)。
- **类型/命名一致**:`AssetClassInput`/`SecurityInput`/`compute_dashboard`/`rebalance_amount`/`net_shares`/`open_lots`/`average_cost` 与 `app/calc.py` 实际签名一致;`AssetClassView` 字段用 `market_value`/`current_weight`/`rebalance_amount`、`Dashboard` 用 `unallocated`/`is_balanced`,均经 `calc.py` 核对。
