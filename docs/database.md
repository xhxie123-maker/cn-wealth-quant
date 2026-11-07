# 数据库设计：CN-WQDS V1.0（PostgreSQL）

> 建表脚本：`sql/schema.sql`。V1.0 默认可以不用数据库（CSV/内存跑通），本设计用于落库、审计与多设备同步。

## 1. 设计原则

1. **时点可追溯**：所有外部数据带 `event_time / published_at / available_at`，查询按 `available_at` 截断。
2. **不可变优先**：行情、财报、因子值以自然键 + 版本写入，不原地改历史；更正如需保留 `superseded_by` 或新版本号。
3. **决策可复现**：`strategy_definitions.params + param_hash`、`run_manifest.config_digest`、`backtest_runs.data_digest` 三者构成复现三要素。
4. **审计可验证**：`audit_log.prev_hash/hash` 形成哈希链；`approval_tokens.signature` 绑定权重与状态。
5. **金额与权重分类型**：金额 `numeric(20,2)`，权重 `numeric(10,6)`，比率 `numeric(12,6)`，避免浮点误差。

## 2. 23 张表

### 2.1 基础数据（6 张）

| # | 表 | 主键 | 说明 |
|---|---|---|---|
| 1 | `instruments` | `instrument_id` | 标的静态信息：类型、交易所、板块、行业、整手、ST、上市/退市日 |
| 2 | `trading_calendar` | `(exchange, trade_date)` | 交易日历，含前后交易日 |
| 3 | `market_bars` | `(instrument_id, trade_date)` | 日行情 + 复权因子 + 三时间戳 |
| 4 | `corporate_actions` | `id` | 分红、拆股、合并等公司行动 |
| 5 | `fundamentals` | `(instrument_id, period_end)` | 财报，含 `published_at/available_at` |
| 6 | `macro_indicators` | `(indicator, period)` | CPI、PMI、政策利率、10Y 收益率等 |

关键索引：`market_bars(trade_date)`、`market_bars(instrument_id, available_at)`、`fundamentals(instrument_id, available_at)`。

### 2.2 因子与状态（4 张）

| # | 表 | 主键 | 说明 |
|---|---|---|---|
| 7 | `factor_definitions` | `factor_code` | 因子元数据：分组、方向、公式、版本 |
| 8 | `factor_values` | `(factor_code, instrument_id, trade_date)` | 原始值、Z-Score、分位、覆盖率 |
| 9 | `factor_snapshots` | `snapshot_id` | 某日某策略的完整因子快照（JSONB） |
| 10 | `market_regimes` | `trade_date` | A/B/C/D 状态与解释变量 |

### 2.3 策略与组合（3 张）

| # | 表 | 主键 | 说明 |
|---|---|---|---|
| 11 | `strategy_definitions` | `(strategy_id, version)` | 策略参数、参数哈希、代码路径、启用标记 |
| 12 | `strategy_signals` | `id`（唯一键 strategy/version/as_of/instrument） | 单标的信号、评分、排名、理由、失效条件 |
| 13 | `target_portfolios` | `id`（唯一键 strategy/version/as_of/instrument） | 策略目标权重与风控后权重、风险状态、审批 token |

### 2.4 风控与审批（5 张）

| # | 表 | 主键 | 说明 |
|---|---|---|---|
| 14 | `risk_policies` | `policy_version` | 风控政策版本（JSONB 全文 + 生效区间） |
| 15 | `risk_assessments` | `id` | 某日某策略的风控评估：状态、指标、检查汇总 |
| 16 | `risk_events` | `id` | 逐项检查明细（code/level/value/limit/message） |
| 17 | `approval_tokens` | `token` | HMAC 审批令牌：权重、状态、摘要、签名、有效期、撤销 |
| 18 | `backtest_runs` | `run_id` | 回测任务元数据与状态 |

> 说明：`risk_events` 依赖 `risk_assessments`；`approval_tokens.token` 同时写入 `target_portfolios.approval_token` 便于联查。

### 2.5 回测结果（3 张）

| # | 表 | 主键 | 说明 |
|---|---|---|---|
| 19 | `backtest_metrics` | `run_id` | 绩效与风险指标（宽表 + 原始 JSONB） |
| 20 | `backtest_trades` | `id` | 成交与被拒订单：费用拆项、状态、拒绝原因 |
| 21 | `decision_cards` | `id` | 决策卡十字段 + 审批 token + 数据时点 |

### 2.6 质量与审计（2 张）

| # | 表 | 主键 | 说明 |
|---|---|---|---|
| 22 | `data_quality_reports` | `id` | 数据质量报告：错误/警告/明细 JSONB |
| 23 | `audit_log` | `(stream_id, seq)` | 哈希链审计日志：事件、载荷、前序哈希、当前哈希 |

## 3. 典型查询

```sql
-- 某日某策略的风控结论与审批
SELECT a.as_of, a.state, a.metrics, t.token, t.signature
FROM risk_assessments a
LEFT JOIN approval_tokens t
  ON t.as_of = a.as_of AND t.strategy_id = a.strategy_id
WHERE a.strategy_id = 'DUAL_MOMENTUM_V1' AND a.as_of = DATE '2026-06-30';

-- 某只标的最近 1 年可用财报（PIT）
SELECT * FROM fundamentals
WHERE instrument_id = '600519.SH'
  AND available_at <= TIMESTAMPTZ '2026-06-30 23:59:59+08'
ORDER BY period_end DESC
LIMIT 4;

-- 被拒订单原因分布
SELECT reject_reason, COUNT(*)
FROM backtest_trades
WHERE run_id = 'BT-20261107-001' AND status = 'REJECTED'
GROUP BY 1 ORDER BY 2 DESC;

-- 审计链校验（应用层逐条重算；SQL 只做展示）
SELECT seq, event_type, prev_hash, hash FROM audit_log
WHERE stream_id = 'demo' ORDER BY seq;
```

## 4. 数据量与分区建议

- `market_bars`：10 标的 × 1300 日 ≈ 1.3 万行/5 年；全市场 5000 标的 × 10 年 ≈ 1200 万行，建议按年分区。
- `factor_values`：标的 × 因子 × 日，量级最大，建议按 `trade_date` 月分区。
- `audit_log`：只追加，按年分区；哈希链校验按 `stream_id` 顺序扫描。
- 历史行情与因子建议保留原始层（raw）与清洗层（clean）两套 schema。

## 5. 迁移与版本

- `risk_policies.payload` 保存完整 YAML 快照，`policy_version` 一旦签发不覆盖。
- `strategy_definitions` 以 `(strategy_id, version)` 为主键，新参数必须新版本号。
- `backtest_runs.config_digest` 与 `run_manifest.json` 一致，保证“同配置可复现”。
