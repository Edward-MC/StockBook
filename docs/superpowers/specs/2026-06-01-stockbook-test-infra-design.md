# StockBook 子项目 E:测试基建 — 设计

> 状态:设计已确认,待写实现计划。
> 目标维度:**测试**。在现有 ~1200 行测试之上,加一层「自动化安全网」——
> CI 跑全套测试、Hypothesis 验 `calc.py` 的核心不变量、覆盖率分层 gate、轻量 lint 守住 Python 3.9 硬约束。
> 不改任何业务行为,纯增测试与基建。

## 1. 背景与动机

当前测试现状:
- `tests/` 下有 `test_calc.py` / `test_api.py` / `test_quotes.py` / `test_rag_*`,共约 1200 行,全部基于例子(example-based)。
- **无 CI**:测试只在本地手动跑,改坏了不会自动拦。
- **无覆盖率度量**:不知道核心计算引擎的分支覆盖到哪。
- **无 lint**:项目硬约束「只用 Python 3.9 语法、不用 `X | None` 等 3.10+ 写法」全靠人记,误写会在 3.9 运行时才炸。

`calc.py` 是纯函数、无框架依赖的核心计算引擎——所有持仓/占比/再平衡都由它推导。
它正是 **property-based testing(基于性质)** 的理想对象:与其举几个例子,不如断言「对任意合法输入,某不变量恒成立」。

## 2. 范围(YAGNI)

**做:**
1. GitHub Actions CI:lint + 全套 pytest + 覆盖率。
2. Hypothesis 核心不变量套件(针对 `calc.py`)。
3. 覆盖率分层:核心模块(`calc.py` + `services.py`)设硬失败线,外围只报告。
4. 轻量 ruff lint(含 pyupgrade,锁 target 3.9)。
5. 独立 `requirements-dev.txt`(测试/lint 工具不污染运行时依赖)。

**不做(本子项目外):**
- 不重构业务代码、不改 API 行为。
- 不给 `quotes.py` / `rag/` 加网络/模型集成测试(成本高、与本子项目目标无关)。
- 不上 mypy / 类型检查(后续可单独加)。
- 不加 pre-commit hook(CI 已是拦截点,本地 hook 后续可选)。

## 3. 关键决策

### D1. CI 平台 = GitHub Actions,单 job,Python 3.9 单版本
远程已在 GitHub(`Edward-MC/StockBook`)。项目**硬钉 Python 3.9**,所以不做多版本矩阵——
跑唯一支持的版本即可。多版本反而会引入「在 3.11 能过、3.9 炸」的噪音(那正是 lint 要拦的)。

### D2. 覆盖率分层:核心严、外围松
- `pytest --cov=app` 跑出**全量**覆盖率报告(只看,不卡)。
- 单独一步 `coverage report --include=app/calc.py,app/services.py --fail-under=95`
  **只对核心两个模块设硬失败线**。低于阈值则 CI 红。
- 为什么不用 `pytest --cov-fail-under`:那是对**全量合并**覆盖率设阈值;
  `quotes.py`/`rag/` 含网络/模型分支,全量很难拉高、且易因外围波动误伤核心 gate。
  用 `coverage report --include=... --fail-under` 把 gate 精确落在「最该保的纯逻辑」上。
- 阈值定 **95%**:留一点余量给边界分支;跑稳后若长期 100% 再收紧。

### D3. dev 依赖独立成 `requirements-dev.txt`
运行时 `requirements.txt` 是项目卖点(`pip install -r requirements.txt && uvicorn` 即跑),
不该被 `pytest-cov`/`hypothesis`/`ruff` 这些**仅开发期**工具撑大。分两个文件:
- `requirements.txt`(不动)= 运行时。
- `requirements-dev.txt`(新增)= `-r requirements.txt` + 测试/lint 工具,钉版本。
CI 装 `requirements-dev.txt`(它会连带运行时)。

### D4. 用 Hypothesis 验**性质**而非举例
`calc.py` 的不变量(占比和、再平衡落点、批次剩余守恒等)对**任意**合法输入都该成立。
example-based 测试只覆盖手挑的几个点;property-based 让 Hypothesis 自动搜索反例(含边界:零总资产、负现金、空仓、单类等),覆盖面与回归防护远强。

### D5. ruff 轻量 lint,锁 target py39
选 `E`/`F`(基础错误)+ `UP`(pyupgrade,能抓 `X | None`、`Optional` 写法等 3.10+ 语法)+ `B`(常见 bug)。
**先按现有代码调到零报错**(必要时 per-file ignore 或收窄规则集),不引入「一墙红」。
核心价值:把「不用 3.10+ 语法」这条人肉约束变成机器拦截。

## 4. 组件

### 4.1 `.github/workflows/ci.yml`(新增)
```yaml
触发: push(main 分支) + pull_request
job: 单个,ubuntu-latest
步骤:
  1. actions/checkout
  2. actions/setup-python@v5,python-version 3.9,cache pip
  3. pip install -r requirements-dev.txt
  4. ruff check .
  5. pytest --cov=app --cov-report=term-missing
  6. coverage report                                          # 全量,只看
  7. coverage report --include=app/calc.py,app/services.py \
       --fail-under=95                                        # 核心 gate
```
> 注:step 5 已生成 `.coverage`,step 6/7 复用它,不重跑测试。

### 4.2 `requirements-dev.txt`(新增)
```
-r requirements.txt
pytest-cov==<pin>
hypothesis==<pin>
ruff==<pin>
```
版本在实现时锁到与 Python 3.9 兼容的具体版本。

### 4.3 `pyproject.toml`(新增,**仅工具配置**)
**不含** `[build-system]` / `[project]` 表 —— 避免被 pip/构建工具当成可安装包,纯粹放工具配置:
- `[tool.ruff]`:`target-version = "py39"`,`line-length`,`select = ["E","F","UP","B"]`,
  按需 `ignore` / `per-file-ignores`(调到现有代码零报错)。
- `[tool.coverage.run]`:`source = ["app"]`,`omit`(排除明显不可测的入口胶水,如有)。
- `[tool.pytest.ini_options]`(可选):收敛 `testpaths` 等,减少根目录散落配置。

### 4.4 `tests/test_calc_properties.py`(新增)
Hypothesis 策略 + 不变量。策略用 `@st.composite` 生成合法的
`AssetClassInput` / `SecurityInput` / 交易序列(含边界:无价 pending、负现金类、空仓、零总资产)。

**核心不变量(对应交付目标):**

| # | 不变量 | 守的是 |
|---|---|---|
| I1 | `compute_dashboard` 后,各**正值大类** `current_weight` 之和 ≈ 100%(`weight_denom>0` 时) | 占比恒等式(决策 D2/§4 `weight_denom`) |
| I2 | 每个 `current_weight` 恒在 `[0, 100]`(含负现金 floor 到 0) | 负现金不污染占比(架构决策 16) |
| I3 | 对每类施加 `rebalance_amount(target, current, denom)` 后,新占比落到 `target_weight` | 再平衡正确性 |
| I4 | `net_shares(txs)` == `Σ open_lots 剩余`(当所有 sell 都已 `matched_buy_id` 配对) | 批次配对守恒(架构决策 14) |
| I5 | `average_cost(txs)` 落在 `[最低买价, 最高买价]`(有未平仓批次时) | 加权均价不越界 |
| I6 | `rebalance_amount` 符号:欠配(current<target)为正、超配为负 | 建议方向正确 |
| I7 | `compute_dashboard` 对任意合法输入**不抛异常**、输出无 `NaN/inf` | 健壮性 / 边界兜底 |
| I8 | 目标占比校验:`Σtarget_weight == 100` ⟺ 「保存目标」通过(未分配=0);未满 100 时未分配≠0 | `PUT /api/strategy/targets` 校验规则(架构决策 5) |

> **重要区分(brainstorm 中澄清)**:I1 管的是**实际占比**(由市值推导、构造上恒等 100%);
> I8 管的是**目标占比**(拨杆,带未分配池,**不一定** 100%,只在保存时要求归 0)。
> 两者是不同的量,不可混为一条。
>
> I8 若对应的校验逻辑在 `schemas.py`/`routers` 而非 `calc.py`,则放到对它最贴近的纯函数处验;
> 实现时确认校验落点,必要时抽一个纯函数承载该规则以便 property 测试。

### 4.5 文档(必须同步,项目铁律)
- `docs/architecture.md`:关键决策新增一条(CI / property-based / 覆盖率分层);功能日志加一行。
- `CLAUDE.md`「命令」区:补 `pip install -r requirements-dev.txt`、`ruff check .`、`pytest --cov=app` + `coverage report`。

## 5. 数据流 / 运行流

```
开发者 push / 开 PR
        │
        ▼
GitHub Actions(ci.yml)
        │
        ├─ ruff check .              ← 3.9 语法 + 基础错误,失败即红
        ├─ pytest --cov=app         ← 全套 example + property 测试
        ├─ coverage report          ← 全量,信息性
        └─ coverage report --include=core --fail-under=95   ← 核心 gate,失败即红
        │
        ▼
绿 = 可合并;红 = 拦住
```

## 6. 测试本子项目自身

- **Hypothesis 测试**:本地 `pytest tests/test_calc_properties.py -q` 全绿;
  故意改坏 `calc.py` 一处(如去掉 `weight_denom` 的 `max(0,...)`),确认对应不变量能**抓到反例**(验证测试有效,不是永真)。
- **覆盖率 gate**:本地 `coverage report --include=app/calc.py,app/services.py` 看实际数字,确认 ≥95% 再定阈值;故意把阈值设到高于实际,确认 gate 会失败(验证 gate 真的在卡)。
- **ruff**:本地 `ruff check .` 零报错;临时写一行 `x: int | None` 确认被 `UP` 规则抓到。
- **CI**:push 到分支后看 Actions 实际跑绿;故意推一个坏改动确认变红(可在 PR 上验证)。

## 7. 风险 / 注意

- **ruff 首次可能报现有代码一堆问题** → 实现时先 `ruff check .` 看清单,要么修、要么收窄规则集/加 ignore,**目标是零报错且不掩盖真问题**。
- **I8 校验落点**:若现有校验不在纯函数里,需小重构抽出纯函数(仅为可测,不改行为)。
- **覆盖率阈值过紧** → 先用实际数字定 95%,别拍脑袋设 100%。
- **绝不碰用户 `stockbook.db`**:property 测试只用内存/临时数据,不连真实库(calc 本就无 DB 依赖,天然安全)。
