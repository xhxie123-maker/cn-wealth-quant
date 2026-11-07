# 软件架构说明：CN-WQDS V1.0

## 1. 总体形态：模块化单体

个人投资者场景不需要微服务。V1.0 采用**模块化单体（modular monolith）**：

- 一个 Python 包、一组 YAML 配置、一条 CLI 主链路；
- 模块之间通过明确的 dataclass / pandas 契约通信，不共享可变全局状态；
- 需要拆服务时，`data / risk / backtest` 可以原样抽成独立进程或 HTTP 服务。

```text
┌────────────────────────────────────────────────────────────┐
│                        CLI / Streamlit                      │
│         generate-sample · demo · backtest · validate · serve │
└───────────────────────────┬────────────────────────────────┘
                            │
┌───────────────────────────▼────────────────────────────────┐
│                    Application Services                      │
│  strategy registry · decision engine · reporting · audit     │
└───────┬───────────────┬───────────────┬──────────────┬──────┘
        │               │               │              │
┌───────▼──────┐ ┌──────▼──────┐ ┌──────▼──────┐ ┌────▼─────┐
│  data layer  │ │factor/portfolio│ │  backtest  │ │   risk   │
│ PIT + quality│ │  compute/opt   │ │ engine/cost│ │ policy   │
└───────┬──────┘ └──────┬──────┘ └──────┬──────┘ └────┬─────┘
        │               │               │              │
┌───────▼───────────────▼───────────────▼──────────────▼─────┐
│         config (system / universe / strategies / risk)      │
│                    + sample & CSV data                       │
└──────────────────────────────────────────────────────────────┘
```

## 2. 模块职责

| 包 | 职责 | 不负责 |
|---|---|---|
| `config.py` | 加载四份 YAML，深路径读取，根目录解析 | 业务规则 |
| `data/calendar.py` | 中国市场交易日历、偏移、月末/周末判断 | 行情生成 |
| `data/schema.py` | `Instrument / Dataset / 三时间戳 / as_of / 面板` | 因子计算 |
| `data/rules.py` | 按（交易所、资产类别、板块、生效日）解析交易规则 | 撮合 |
| `data/providers.py` | 合成数据、CSV、AkShare 三种数据源 | 质量判断 |
| `data/quality.py` | 9 类质量检查与报告 | 修复数据 |
| `factors/engine.py` | MAD 缩尾、Z-Score、行业中性、方向统一、加权、贡献度 | 选股 |
| `factors/compute.py` | 价值/质量/动量/风险因子与协方差 | 组合优化 |
| `portfolio/optimizer.py` | 协方差收缩、等权/逆波动/风险平价/最小方差、约束投影 | 交易 |
| `strategies/` | 四个策略，输出 `TargetPortfolio`（权重+理由+失效条件） | 下单、风控 |
| `backtest/costs.py` | 佣金、印花税、过户费、滑点、冲击、成交价、可成交量 | 策略 |
| `backtest/engine.py` | 逐日撮合、T+1、涨跌停、停牌、整手、现金闸门、分红拆股 | 绩效归因 |
| `backtest/validation.py` | Walk-forward、CSCV PBO、指标对比 | 参数搜索 |
| `risk/metrics.py` | 绩效与风险指标（CAGR/波动/Sharpe/MDD/VaR/ES/HHI…） | 限额判断 |
| `risk/limits.py` | 状态机、逐项检查、裁剪、HMAC 审批 | 策略逻辑 |
| `risk/stress.py` | 五个压力情景 | 历史回放 |
| `decision/engine.py` | 决策卡十字段、批次、审批校验 | 交易执行 |
| `audit/log.py` | JSONL 哈希链、策略快照 | 数据库 |
| `reporting.py` | Markdown/JSON/CSV 落盘 | 计算 |
| `ui/app.py` | Streamlit 只读决策台 | 写操作 |

## 3. 主链路（`demo`）

```text
load config
  └─ load dataset (CSV/sample, PIT)
      └─ quality checks ──────────────► quality_report.*
      └─ backtest
          ├─ for each trading day:
          │    ├─ mark-to-market, cash, positions
          │    ├─ rebalance date? (monthly)
          │    │    ├─ strategy.generate(ctx)  → target weights + rationale
          │    │    ├─ risk.review_target(...) → approved weights + approval
          │    │    ├─ orders: next_open, T+1, limits, lots, cash gate
          │    │    └─ fills + costs + rejected orders
          │    └─ corporate actions (dividend/split)
          └─ metrics / nav / trades / targets
      └─ latest decision (as_of last day)
          ├─ strategy target
          ├─ risk review + HMAC approval
          └─ decision cards（十字段）
      └─ stress tests
      └─ run_manifest + audit chain
```

## 4. 关键契约

### 4.1 Dataset

```python
Dataset:
  instruments: DataFrame[instrument_id, name, asset_type, exchange, board, industry, ...]
    bars: DataFrame[instrument_id, trade_date, open, high, low, close, volume, amount,
                     adj_factor, event_time, published_at, available_at]
    fundamentals: DataFrame[instrument_id, period_end, published_at, available_at, roe, ...]
    corporate_actions: DataFrame[instrument_id, ex_date, action_type, value, ...]
    macro: DataFrame[indicator, period, published_at, available_at, value]
```

- `as_of(date)`：按 `available_at <= date 23:59:59` 截断；
- `price_panel(field, adjusted=True)`：返回 `trade_date × instrument_id` 面板；
- `fundamentals_asof(date)`：每个标的取最近一期已可用财报。

### 4.2 TargetPortfolio

```python
TargetPortfolio:
  as_of, strategy_id, strategy_name, version
  weights: Series[instrument_id → weight]   # 和 ≤ 1，>1 直接抛错
  scores, rationale, invalidation, horizon, diagnostics, metadata
```

### 4.3 RiskDecision

```python
RiskAssessment: state, checks, metrics, system_halt
RiskApproval:   token, signature, expires_at, payload_digest
RiskDecision:   assessment, approved_weights, requested_weights, rejected, reasons, approval
```

### 4.4 DecisionCard

固定十字段 + 策略版本 + 免责声明。缺审批时 `DecisionEngine.build()` 抛 `RiskApprovalError`。

## 5. 关键设计决策

| 决策 | 原因 | 代价 |
|---|---|---|
| 零 scipy | 个人电脑安装 scipy 常失败；风险平价/最小方差/单纯形投影可自实现 | 代码更多，需要测试覆盖 |
| 纯 pandas/numpy 回测 | 可读、可调试、依赖少 | 大规模参数搜索慢 |
| YAML 风控独立 | 策略不可覆盖，参数可审计 | 配置与代码需同步版本 |
| HMAC 审批令牌 | 防止“先出建议、后补风控” | 需管理密钥（示例用环境变量/默认开发密钥） |
| 复合主键 + 三时间戳 | PIT 与审计 | 数据体积略大 |
| 只读 Streamlit | 避免界面误操作下单 | 不能从界面改参数 |

## 6. 失败模式与处理

| 失败 | 检测 | 处理 |
|---|---|---|
| 数据缺失/过期 | `run_quality_checks` + `max_stale_days` | 错误阻断；警告入报告 |
| 未来数据 | `available_at` 截断 | `as_of()` 不返回 |
| 涨跌停/停牌 | 规则簿 + 行情 | 拒单，写 `rejected_orders.csv` |
| 现金不足 | 现金闸门 | 按可用现金缩量，记录拒单/缩量原因 |
| 风控越限 | `RiskEngine.assess` | 裁剪、降险、HALT |
| 审批缺失/篡改 | `RiskApproval.verify` | 抛 `RiskApprovalError` |
| 运行失败 | CLI 异常 | 非零退出码 + 日志 + 保留中间产物 |

## 7. 扩展点

- **数据源**：实现 `BaseProvider.load(start, end)` 返回 `Dataset`，注册到 CLI。
- **策略**：继承/实现 `generate(ctx) -> TargetPortfolio`，在 `strategies.yaml` 注册。
- **风控项**：在 `RiskEngine.assess` 增加 `RiskCheck`，在 YAML 增加阈值。
- **优化器**：在 `portfolio/optimizer.py` 增加函数，策略按名称调用。
- **落库**：用 `sql/schema.sql` 建表，把 `write_json/write_frame` 替换为 Repository。

## 8. 部署

- 本地：Python venv + `pip install -e .`；数据与报告均在项目目录。
- 研究机/服务器：同一包 + PostgreSQL；Streamlit 只读挂载 `reports/`。
- CI：`pytest` + `ruff`；可选 nightly `validate` 跑 Walk-forward/PBO。

## 9. 安全与合规

- 不存券商账号、不存交易密码、不接下单 API。
- 审批密钥从环境变量读取；示例默认密钥仅用于演示，生产必须替换。
- 报告与界面固定免责声明。
- 日志不记录个人身份信息；审计日志只记录策略、权重、状态、哈希。
