# 验收清单：CN-WQDS V1.0

- 验收日期：2026-11-07
- 验收环境：Windows / Python 3.13.16 / numpy 2.5.4 / pandas 3.1.0 / PyYAML 6.0.3 / pytest 9.2.0 / Streamlit 1.67.0
- 示例数据：`seed=20261107`，10 个虚构标的，2021-01-04 至 2026-06-30，1330 个交易日

## 1. 复现命令

```powershell
Set-Location <项目根>
$env:PYTHONPATH="$PWD\src"

python scripts/check_env.py
python -m wealth_quant.cli generate-sample
python -m wealth_quant.cli demo --strategy DUAL_MOMENTUM_V1 --out reports/demo
python -m pytest -q
python scripts/verify_reports.py reports/demo
```

## 2. 验收结果

| 编号 | 验收项 | 判定 | 证据 |
|---|---|---|---|
| A1 | 示例数据可复现、无未来数据 | 通过 | `generate-sample` 生成 10 标的；`tests/test_data.py::test_as_of_is_point_in_time` |
| A2 | 三时间戳齐全且顺序正确 | 通过 | `test_synthetic_dataset_shape`（`available_at >= published_at >= event_time`） |
| A3 | 数据质量检查可阻断/告警 | 通过 | `test_quality_passes_on_clean_sample`、`test_quality_detects_duplicate_bars`；`reports/demo/quality_report.json` |
| A4 | 规则簿按板块/资产/生效日解析 | 通过 | `tests/test_rules.py` 6 个用例：主板 10%、创业板/科创板 20%、ST 5%、股票 ETF T+1、债券/货币 ETF T+0、2026-07-06 占位规则 `verified=False` |
| A5 | 组合约束投影不退化 | 通过 | `test_projection_respects_single_asset_cap_and_redistributes`：90% 单资产被裁剪后总仓位回到 60%（三资产全触顶），而非退化为现金 |
| A6 | 回测 next-open 成交 | 通过 | `test_next_open_means_fills_after_signal_date`：首笔成交日晚于信号日 |
| A7 | 涨停不可买入 | 通过 | `test_limit_up_rejects_buy_order`：构造 +12% 一字板，`rejected` 中出现 `limit_up` |
| A8 | 风控先投影再评估、RED 压降敞口 | 通过 | `test_single_asset_cap_is_projected_before_assessment`、`test_red_state_uses_current_drawdown_and_caps_exposure`（当前回撤 27% → RED，总敞口 ≤ 40%） |
| A9 | 风控一票否决与审批防篡改 | 通过 | `test_system_halt_blocks_new_positions`、`test_approval_detects_tampered_weights`（改权重/改日期/改策略均失败） |
| A10 | 决策卡十字段完整 | 通过 | `scripts/verify_reports.py` 校验 6 张卡；`test_decision_cards_contain_required_fields` |
| A11 | 审计链不可篡改 | 通过 | `test_audit_chain_detects_tampering`；`reports/demo/audit_log.jsonl` 的 `verify() = True` |
| A12 | 样本外与 PBO 可运行 | 通过 | `python -m wealth_quant.cli validate --strategy DUAL_MOMENTUM_V1 --out reports/validation`：样本内 CAGR -5.33%、样本外 CAGR -0.49%、PBO **0.496**（9 组参数、252 个 IS/OOS 组合）；结果见 `reports/validation/DUAL_MOMENTUM_V1_validation.json` |
| A13 | Streamlit 决策台可启动 | 通过 | headless 启动后 `/_stcore/health` 返回 `ok`，首页 HTTP 200 |
| A14 | 一键脚本可用 | 通过 | `scripts/quickstart.ps1`（环境自检 → 生成数据 → demo → 验收） |

## 3. 自动化测试

`python -m pytest -q`：**31 passed**（约 30 秒）。

| 文件 | 覆盖 |
|---|---|
| `tests/test_data.py` | 数据形状、三时间戳、PIT 截断、质量检查、重复行情 |
| `tests/test_rules.py` | 主板/创业板/科创板/ST 涨跌幅、T+1/T+0、2026 规则切换 |
| `tests/test_risk.py` | 单资产上限投影、RED 当前回撤、SYSTEM_HALT、审批篡改 |
| `tests/test_portfolio_validation.py` | 权重投影/行业上限、目标权重和 >100% 报错、Walk-forward 窗口、CSCV PBO、审计链 |
| `tests/test_backtest.py` | next-open 成交、涨停拒单、涨跌停表来自规则簿 |
| `tests/test_decision_costs.py` | 最低佣金、印花税、滑点方向、冲击成本、缺审批报错、决策卡字段 |

## 4. 端到端示例结果（DUAL_MOMENTUM_V1）

| 指标 | 数值 |
|---|---|
| 区间 | 2021-01-04 至 2026-06-30（1330 个交易日） |
| 年化收益 | -3.93% |
| 年化波动 | 5.21% |
| Sharpe / Sortino | -1.03 / -1.31 |
| 最大回撤 | -22.84%（峰 2022-05-18，谷 2026-06-03） |
| Calmar | -0.17 |
| VaR 95% / ES 95% | -0.31% / -0.93% |
| 年化换手 | 127.32% |
| 胜率 / 成交笔数 | 41.18% / 108 |
| 期末净值 | 809,572.01（-19.04%） |

期末风控状态 **RED**：当前回撤 22.1%，超过紧急阈值 18%，总敞口被压到 20%，决策卡全部为 RED，只允许在 20% 以内做资产间轮动。

> 以上为合成数据上的工程验证结果，**不是真实业绩，也不代表策略优劣**。

## 5. 未覆盖项与上线前动作

| 项目 | 现状 | 上线前必须做 |
|---|---|---|
| 真实行情/财报 | 仅合成数据与 CSV 接口 | 接 AkShare 或券商数据，逐字段核对 `published_at/available_at` |
| 2026-07-06 交易新规 | 占位规则，`verified=False` | 用交易所正式文件校准涨跌幅、T+0/T+1、申报价格笼子 |
| 个人债券 | V1.0 只做债券 ETF | 如接入个券，先做合格投资者准入与适当性核验 |
| 压力测试 | 5 个参数化情景 | 增加历史情景回放与相关性突变 |
| 参数寻优 | PBO 仅对动量类 9 组参数 | 扩大网格时同步控制计算量与多重检验 |
| 实盘对账 | 无交易接口 | 纸上交易 3 个月，比较建议价与真实成交价 |
| 数据库 | DDL 已提供，核心链路未强依赖 | 落库后再做多设备同步与增量更新 |

## 6. 已知限制

1. 合成数据近似零漂移、高波动，四个策略示例均为负收益；这是数据特性，不是策略 bug。
2. `reports/demo/rejected_orders.csv` 只有表头：合成价格路径没有触发涨跌停；涨停拒单路径由 `test_limit_up_rejects_buy_order` 单独构造验证。
3. Streamlit 为只读决策台，不支持在界面改参数或下单。
4. 审计链防的是“事后篡改”，不防“持有密钥者重新签整条链”；生产环境需把密钥放入 KMS/环境变量并定期轮换。
