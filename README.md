# 中国市场稳健理财量化投资辅助决策系统（CN-WQDS）

> **定位**：面向个人投资者的「辅助决策」工具，不是自动交易系统。
> 系统只负责**算信号、做风控、出建议、留证据**；是否下单、下多少，必须由投资者本人在券商 APP 手工完成。

- 市场：中国 A 股 + 场内 ETF（含债券 ETF / 国债 ETF / 货币 ETF / 黄金 ETF）
- 风格：稳健型，中期持有（1–12 个月），月度调仓
- 资金：个人自有资金，人民币计价，**不加杠杆、不做空**
- 技术：Python 3.11+ / pandas / FastAPI 风格的分层代码 / Streamlit / PostgreSQL / DuckDB（可选）
- 版本：V1.0.0

---

## 一、为什么做这套东西

个人做量化最常见的三个坑：

1. **未来函数**：用今天才知道的财报、用收盘后才公布的消息去回测历史。
2. **回测很美、实盘很惨**：忽略涨跌停无法成交、停牌、T+1、整手、印花税、滑点、冲击成本。
3. **风控写在策略里**：策略自己说“我很稳”，没有独立的一票否决。

本系统把这三件事拆开解决：

- 所有数据带 **event_time / published_at / available_at** 三时间戳，查询强制 `available_at <= 决策时点`（point-in-time）。
- 回测引擎按交易所规则逐日撮合：**T+1、涨跌停拒单、停牌不可成交、100 股整手、现金闸门、分红拆股、印花税/过户费/滑点/平方根冲击**。
- **风控策略独立成 YAML**，风控引擎拥有对策略目标的裁剪权与一票否决权；策略代码无法覆盖风控结论。
- 关键结论写入**哈希链审计日志**，事后可验证“建议有没有被改过”。

---

## 二、功能矩阵（对应建设手册 8 大模块）

| # | 模块 | 本仓库落点 | 关键能力 |
|---|---|---|---|
| 1 | 数据采集中心 | `src/wealth_quant/data/` | 三时间戳、PIT 截断、9 类质量检查、CSV/AkShare/合成数据三种 Provider |
| 2 | 市场状态识别 | `data/schema.py`、`factors/compute.py`、`ui/app.py` | 用宽基趋势 + 实现波动 + 宏观（CPI/PMI/利率/10Y 收益率）描述 A/B/C/D 状态；状态只用于解释与仓位约束，不直接预测 |
| 3 | 因子体系 | `factors/` | 价值 5 因子、质量 6 因子、动量 6 因子、风险 6 因子；MAD 缩尾 → 横截面 Z-Score → 行业中性 → 方向统一 → 加权合成，保留贡献度 |
| 4 | 策略与组合优化 | `strategies/`、`portfolio/` | 双动量、价值质量、低波核心、ETF 轮动；等权/逆波动/风险平价/最小方差；单资产/行业/总仓位约束投影（纯 numpy，无 scipy） |
| 5 | 回测验证中心 | `backtest/` | next-open 成交、T+1、涨跌停/停牌/整手/现金闸门、分红拆股、完整成本模型；Walk-forward、CSCV PBO |
| 6 | 五级风控 | `risk/` | GREEN / YELLOW / RED / SYSTEM_HALT 状态机；单资产、行业、集中度、相关性、VaR/ES、波动突升、回撤、换手、数据质量、系统对账；**一票否决 + HMAC 审批令牌** |
| 7 | 决策可视化 | `decision/`、`ui/app.py` | 决策卡固定十字段；Streamlit 六页面；Markdown/JSON/CSV 三份报告 |
| 8 | 运行监控与迭代 | `audit/`、`cli.py`、`scripts/` | run_manifest（配置摘要 + 策略快照）、审计哈希链、质量报告、压力测试 |

---

## 三、工程结构

```text
cn-wealth-quant/
├─ config/
│  ├─ system.yaml            # 主配置：数据、成本、回测区间、组合约束
│  ├─ risk_policy.yaml       # 独立风控政策（策略不可覆盖）
│  ├─ strategies.yaml        # 四个策略及参数（含参数哈希）
│  └─ universe.yaml          # 示例投资域（10 个虚构标的）
├─ src/wealth_quant/
│  ├─ config.py              # 配置加载 + 深路径读取
│  ├─ logging_utils.py
│  ├─ cli.py                 # generate-sample / demo / backtest / validate / serve
│  ├─ reporting.py           # Markdown / JSON / CSV 输出
│  ├─ data/                  # 日历、Schema、规则簿、数据源、质量检查
│  ├─ factors/               # 因子计算与合成引擎
│  ├─ portfolio/             # 协方差收缩与四种优化器
│  ├─ strategies/            # 四个策略 + 注册表
│  ├─ risk/                  # 指标、限额、压力测试
│  ├─ backtest/              # 成本模型、撮合引擎、Walk-forward/PBO
│  ├─ decision/              # 决策卡引擎（十字段）
│  ├─ audit/                 # 哈希链审计日志
│  └─ ui/app.py              # Streamlit 决策台（六页面）
├─ sql/schema.sql            # PostgreSQL 23 张核心表 + 索引
├─ docs/                     # PRD、架构、数据库、验收
├─ tests/                    # pytest 用例
├─ scripts/                  # 一键演示 / 环境自检
├─ data/sample/              # 可复现的离线示例数据
└─ reports/demo/             # 示例运行产物
```

---

## 四、快速开始

### 4.1 环境

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -e .
# 可选：界面 / 测试 / 数据源
pip install -e ".[ui,dev,data]"
```

要求 Python ≥ 3.11；核心只依赖 `numpy`、`pandas`、`PyYAML`，**不依赖 scipy**（风险平价、最小方差、单纯形投影均为自实现）。

### 4.2 三条命令跑通

```powershell
# 1) 生成离线示例数据（固定 seed，可复现）
python -m wealth_quant.cli generate-sample

# 2) 端到端演示：数据质量 → 回测 → 风控 → 决策报告 → 压力测试 → 审计
python -m wealth_quant.cli demo

# 3) 打开决策台
python -m wealth_quant.cli serve
# 或 streamlit run src/wealth_quant/ui/app.py
```

也可以不安包装，直接：

```powershell
$env:PYTHONPATH="$PWD\src"
python -m wealth_quant.cli demo --strategy DUAL_MOMENTUM_V1 --out reports/demo
```

### 4.3 其他命令

```powershell
# 单策略回测，输出到指定目录
python -m wealth_quant.cli backtest --strategy ETF_ROTATION_V1 --out reports/etf

# 样本外验证：Walk-forward + CSCV PBO（建议在 CI 或研究机运行）
python -m wealth_quant.cli validate --strategy DUAL_MOMENTUM_V1
```

---

## 五、示例运行产物

`reports/demo/` 每次运行都会重新生成：

| 文件 | 内容 |
|---|---|
| `metrics.json` | CAGR、年化波动、Sharpe/Sortino、最大回撤、Calmar、VaR/ES、Beta/Alpha、IR、换手、胜率、成交笔数 |
| `nav.csv` | 逐日净值与基准 |
| `trades.csv` / `rejected_orders.csv` | 成交明细 / 被拒订单（涨跌停、停牌、现金不足、整手为 0 等） |
| `target_history.json` | 每期策略目标权重、风控裁剪后权重、风控原因 |
| `decision_cards.csv` / `decision_batch.json` | 决策卡（十字段） |
| `decision_report.md` | 可读的中文决策报告 |
| `risk_snapshot.json` | 风控状态、逐项检查、指标 |
| `stress_tests.json` | 五个压力情景的组合冲击 |
| `quality_report.json/.csv` | 数据质量检查 |
| `run_manifest.json` | 配置摘要、策略快照、指标快照 |
| `audit_log.jsonl` | 哈希链审计日志（`verify()=True`） |

> **示例数据是合成的**（`seed=20261107`，四种市场状态轮转、含分红与 2023-06-15 拆股），只用于验证工程链路，**不代表任何真实市场或未来业绩**。由于合成市场近似零漂移且波动较大，示例策略跑出负收益属于正常现象；请勿把示例数字当作策略优劣结论。

一次示例运行（DUAL_MOMENTUM_V1，2021-01-04 至 2026-06-30）：

| 指标 | 数值 |
|---|---|
| 年化收益 | -3.93% |
| 年化波动 | 5.21% |
| Sharpe | -1.03 |
| 最大回撤 | -22.84% |
| 年化换手 | 127.32% |
| 成交笔数 | 108 |
| 期末净值 | 809,572.01 |

期末风控状态为 **RED**（当前回撤 22.1% 触发紧急阈值），总敞口被压到 20%，决策卡只允许在 20% 以内做资产间轮动——这是风控生效的正常表现，不是程序错误。

样本外验证（`validate`，9 组动量参数、252 个 IS/OOS 组合）：样本内 CAGR -5.33%，样本外 -0.49%，**PBO 0.496**。PBO 接近 0.5 说明“样本内最优参数在样本外不再最优”的概率很高，即参数选择没有稳定优势；这与合成数据近似零漂移的特性一致，也说明不应把示例参数直接用于实盘。

---

## 六、关键设计

### 6.1 数据：三时间戳 + PIT

| 字段 | 含义 |
|---|---|
| `event_time` | 事件真实发生时间（成交、财报期末） |
| `published_at` | 数据对外发布时间 |
| `available_at` | 数据进入本系统、可供决策使用的时间（含采集延迟） |

`Dataset.as_of(date)` 只保留 `available_at <= 决策时点` 的记录，财报还要满足公告滞后（示例中 `published_at = 期末 + 45 天`）。这是避免未来函数的第一道闸门。

### 6.2 交易规则簿：按（交易所、资产类别、板块、生效日）解析

- 股票：T+1，主板 ±10%，创业板/科创板 ±20%，ST ±5%
- 股票 ETF：T+1，±10%
- 债券 ETF / 货币 ETF / 黄金 ETF / 跨境 ETF：T+0（按规则簿配置）
- **2026-07-06 沪深交易新规**以占位规则写入，`verified=False`，必须在生效前用交易所正式文件核验后再切换

回测引擎不写死规则，一律通过 `TradingRuleBook.rule_for(instrument, date)` 取当日有效规则。

### 6.3 风控：独立、可裁剪、一票否决

- 风控参数在 `config/risk_policy.yaml`，与策略代码物理分离。
- `RiskEngine.review_target()` 对策略目标做：结构投影（单资产 ≤ 20%、行业 ≤ 35%、总仓位 ≤ 98%）→ 风险评估 → RED 降险 → 二次评估 → 签发 HMAC 审批令牌。
- 策略代码只能提交目标，不能修改风控结论；`strategy_cannot_override: true`。
- 决策引擎缺审批令牌直接抛 `RiskApprovalError`；令牌被篡改或过期，`verify_approval()` 返回失败。

五级状态：

| 状态 | 触发 | 行为 |
|---|---|---|
| GREEN | 全部检查通过 | 正常执行风险预算 |
| YELLOW | 回撤 ≥ 8%、波动突升、相关性偏高 | 提示 + 收紧但不否决 |
| RED | 回撤 ≥ 12%、VaR/ES 超限、集中度超限 | 总敞口压到 min(max(当前, 20%), 40%)，只允许轮动 |
| SYSTEM_HALT | 数据中断、净值对账失败、风控服务异常 | 拒绝生成任何可执行调整 |

> RED 用**当前回撤**（相对历史峰值），而不是历史最大回撤；否则一旦创出深回撤，即使净值修复也会被永久锁死。另设 20% 防御下限，避免“清仓 → 无法修复 → 永远 RED”的死锁。

### 6.4 回测四道关卡

1. **数据关**：PIT、完整性、复权、停牌、异常收益、财务字段覆盖率。
2. **成交关**：next-open、T+1、涨跌停/停牌拒单、整手、现金闸门、成交量参与率上限。
3. **成本关**：佣金（最低 5 元）、印花税（仅股票卖出）、过户费、滑点、平方根冲击。
4. **样本外关**：Walk-forward（训练 504 / 测试 126 / 步长 126）与 CSCV PBO（10 分区），输出过拟合概率。

### 6.5 审计链

每次运行把关键事件按 JSONL 追加，`prev_hash` 串联，任何一条被改动都会导致后续哈希不匹配：

```powershell
python -c "from wealth_quant.audit import AuditLog; print(AuditLog('reports/demo/audit_log.jsonl').verify())"
```

### 6.6 决策卡十字段

`资产 / 信号 / 当前仓位 / 目标仓位 / 建议金额 / 风险等级 / 决策理由 / 失效条件 / 持有期限 / 数据时点`，并附策略版本、人工确认标记、免责声明。

---

## 七、23 张核心表

PostgreSQL 建表脚本见 `sql/schema.sql`，表说明见 `docs/database.md`。

`instruments`、`trading_calendar`、`market_bars`、`corporate_actions`、`fundamentals`、`macro_indicators`、`factor_definitions`、`factor_values`、`factor_snapshots`、`market_regimes`、`strategy_definitions`、`strategy_signals`、`target_portfolios`、`risk_policies`、`risk_assessments`、`risk_events`、`approval_tokens`、`backtest_runs`、`backtest_metrics`、`backtest_trades`、`decision_cards`、`data_quality_reports`、`audit_log`。

V1.0 默认不启用数据库：核心链路用 CSV/内存即可跑通；接 PostgreSQL 时把 `data.source` 改为 `csv` 或实现新的 Provider，并把 `run_manifest`、`risk_assessments`、`decision_cards`、`audit_log` 落库。

---

## 八、验收清单

完整清单见 `docs/acceptance.md`，核心几条：

- [x] `generate-sample` 可复现生成 10 标的、2021-01-04 至 2026-06-30 的日频数据
- [x] `demo` 一条命令产出全部报告，审计链 `verify() = True`
- [x] 回测拒绝涨跌停/停牌订单，记录在 `rejected_orders.csv`
- [x] 单资产 30% 目标被裁剪到 20%，并在决策理由中说明
- [x] 回撤触发 RED 后总敞口被压降，决策卡风险等级同步为 RED
- [x] 审批令牌被篡改时 `verify_approval()` 返回 `False`
- [x] 缺失 `available_at` 的未来数据不会进入 `as_of()` 结果
- [x] PBO / Walk-forward 可运行并输出样本外指标

---

## 九、上线前必须做的五件事

1. **替换数据源**：示例数据是合成的。接 AkShare 或券商/Wind/聚宽，逐字段核对三时间戳，尤其是财报 `available_at`。
2. **核验规则簿**：2026-07-06 新规目前是占位版本（`verified=False`），必须用交易所原文校准涨跌幅、T+0/T+1、申报价格笼子等。
3. **校准风控参数**：`risk_policy.yaml` 的阈值只是起点，要按个人风险承受能力、资金规模、持仓集中度重新设定。
4. **做压力测试与纸上交易**：至少 3 个月纸上跟踪，比较“模型建议价”和“真实成交价”的偏差。
5. **明确边界**：系统不下单、不接券商交易接口、不构成投资建议；所有交易由投资者本人确认。

---

## 十、免责声明

本项目仅用于技术研究与个人投资辅助，**不构成任何投资建议**。历史回测、模拟结果不代表未来收益，市场存在本金损失风险。使用前请阅读 `docs/PRD.md` 的边界章节，并咨询有资质的专业人士。

## License

MIT，见 `LICENSE`。
