"""命令行入口：生成示例数据、运行演示、回测、验证、启动界面。

用法：
    python -m wealth_quant.cli demo
    python -m wealth_quant.cli generate-sample
    python -m wealth_quant.cli backtest --strategy DUAL_MOMENTUM_V1
    python -m wealth_quant.cli validate --strategy DUAL_MOMENTUM_V1
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from .audit.log import AuditLog, StrategyRegistrySnapshot
from .backtest.costs import CostModel
from .backtest.engine import BacktestConfig, Backtester
from .backtest.validation import cscv_pbo, split_is_oos
from .config import ProjectConfig
from .data.providers import CSVProvider, generate_synthetic_dataset
from .data.quality import run_quality_checks
from .data.rules import TradingRuleBook
from .data.schema import Dataset
from .decision.engine import DecisionEngine
from .logging_utils import get_logger
from .reporting import decision_markdown, write_frame, write_json
from .risk.limits import RiskEngine
from .risk.metrics import summarize
from .risk.stress import run_stress_tests
from .strategies.base import StrategyContext
from .strategies.registry import StrategyRegistry

log = get_logger(__name__)


def _as_date(value, default: str) -> date:
    """YAML 会把 2021-01-04 解析为 date，这里统一兜底。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value or default))


def _load_dataset(cfg: ProjectConfig, prefer_csv: bool = True) -> Dataset:
    sample_dir = cfg.resolve(cfg.get("data.sample_dir", "data/sample"))
    if prefer_csv and (sample_dir / "bars.csv").exists():
        return CSVProvider(sample_dir).load()
    log.info("未找到示例 CSV，使用内置合成数据（仅用于演示）")
    return generate_synthetic_dataset(cfg.universe.get("instruments", []),
                                      seed=int(cfg.get("data.seed", 20261107)))
def _write_sample(cfg: ProjectConfig) -> Dataset:
    """生成并落盘示例数据（可复现）。"""
    dataset = generate_synthetic_dataset(
        cfg.universe.get("instruments", []),
        start=_as_date(cfg.get("backtest.start", "2021-01-04"), "2021-01-04"),
        end=_as_date(cfg.get("backtest.end", "2026-06-30"), "2026-06-30"),
        seed=int(cfg.get("data.seed", 20261107)),
    )
    out = cfg.resolve(cfg.get("data.sample_dir", "data/sample"))
    out.mkdir(parents=True, exist_ok=True)
    write_frame(out / "instruments.csv", dataset.instruments.reset_index(drop=True))
    write_frame(out / "bars.csv", dataset.bars)
    write_frame(out / "fundamentals.csv", dataset.fundamentals)
    write_frame(out / "corporate_actions.csv", dataset.corporate_actions)
    write_frame(out / "macro.csv", dataset.macro)
    write_frame(out / "trading_calendar.csv", pd.DataFrame({"trade_date": [d for d in dataset.calendar.dates]}))
    if dataset.benchmark is not None and len(dataset.benchmark):
        bench = dataset.benchmark.reset_index()
        bench.columns = ["trade_date", "close"]
        write_frame(out / "benchmark.csv", bench)
    log.info("示例数据已写入 %s", out)
    return dataset


def _build_risk_engine(cfg: ProjectConfig) -> RiskEngine:
    return RiskEngine(cfg.risk_policy)


def _backtest(cfg: ProjectConfig, dataset: Dataset, strategy_id: str, risk_engine: RiskEngine) -> object:
    strategy = StrategyRegistry(cfg.strategies).build(strategy_id)
    config = BacktestConfig(
        start=_as_date(cfg.get("backtest.start", "2021-01-04"), "2021-01-04"),
        end=_as_date(cfg.get("backtest.end", "2026-06-30"), "2026-06-30"),
        initial_cash=float(cfg.get("portfolio.initial_cash", 1_000_000)),
        rebalance_frequency=str(cfg.get("portfolio.rebalance_frequency", "monthly")),
        lot_size_default=int(cfg.get("portfolio.lot_size_default", 100)),
        min_trade_notional=float(cfg.get("portfolio.min_trade_notional", 1000)),
        cash_buffer=float(cfg.get("portfolio.cash_buffer", 0.02)),
        risk_free_rate=float(cfg.get("market.risk_free_rate", 0.015)),
        benchmark=str(cfg.get("market.benchmark", "000300.SH")),
    )
    return Backtester(
        dataset=dataset, strategy=strategy, config=config,
        cost_model=CostModel.from_config(cfg.system.get("costs", {})),
        rulebook=TradingRuleBook.with_2026_revision(),
        risk_engine=risk_engine, risk_policy=cfg.risk_policy,
    ).run()


def _latest_decision(cfg: ProjectConfig, dataset: Dataset, strategy_id: str,
                     risk_engine: RiskEngine, backtest_result=None):
    as_of = dataset.calendar.last
    ctx_dataset = dataset.as_of(as_of)
    universe = ctx_dataset.universe(
        include_types=cfg.get("universe.include_asset_types", None),
        exclude_st=bool(cfg.get("universe.exclude_st", True)),
        max_instruments=cfg.get("universe.max_instruments"),
    )
    strategy = StrategyRegistry(cfg.strategies).build(strategy_id)
    target = strategy.generate(StrategyContext(dataset=ctx_dataset, as_of=as_of, universe=universe,
                                               benchmark=ctx_dataset.benchmark))

    nav_series = backtest_result.nav if backtest_result is not None else pd.Series(dtype=float)
    nav = float(nav_series.iloc[-1]) if len(nav_series) else float(cfg.get("portfolio.initial_cash", 1_000_000))
    current = pd.Series(dtype=float)
    if backtest_result is not None and backtest_result.positions is not None and not backtest_result.positions.empty:
        last_rows = backtest_result.positions[backtest_result.positions["date"] == backtest_result.positions["date"].max()]
        current = pd.Series(last_rows.set_index("instrument_id")["weight"], dtype=float)

    quality = run_quality_checks(ctx_dataset, as_of=as_of,
                                 max_stale_days=int(cfg.get("data.max_stale_days", 3)))
    correlation = ctx_dataset.returns_panel(adjusted=True).tail(252).corr()
    risk = risk_engine.review_target(
        as_of=as_of, target_weights=target.weights, current_weights=current,
        sector_map={i: str(ctx_dataset.instruments.loc[i, "industry"]) for i in ctx_dataset.instruments.index},
        nav_history=nav_series, quality=quality, nav_value=nav,
        correlation_matrix=correlation, strategy_id=target.strategy_id,
    )
    prices = ctx_dataset.price_panel("close", adjusted=False).iloc[-1].to_dict()
    names = {i: str(ctx_dataset.instruments.loc[i, "name"]) for i in ctx_dataset.instruments.index}
    lots = {}
    rules = TradingRuleBook.with_2026_revision()
    for i in ctx_dataset.instruments.index:
        lots[i] = rules.rule_for(ctx_dataset.instrument(i), as_of).lot_size

    engine = DecisionEngine(risk_engine,
                            min_weight_change=float(cfg.get("decision.min_weight_change", 0.02)),
                            max_cards=int(cfg.get("decision.max_cards", 20)),
                            require_risk_approval=bool(cfg.get("decision.require_risk_approval", True)))
    batch = engine.build(as_of=as_of, target=target, risk=risk, current_weights=current, nav=nav,
                         prices=prices, names=names, lot_sizes=lots)
    stress = run_stress_tests(risk.approved_weights, asset_types={
        i: str(ctx_dataset.instruments.loc[i, "asset_type"]) for i in ctx_dataset.instruments.index
    })
    return batch, risk, quality, stress


def cmd_generate_sample(cfg: ProjectConfig, args: argparse.Namespace) -> int:
    _write_sample(cfg)
    return 0


def cmd_demo(cfg: ProjectConfig, args: argparse.Namespace) -> int:
    dataset = _load_dataset(cfg, prefer_csv=True)
    out = cfg.resolve(args.out or "reports/demo")
    out.mkdir(parents=True, exist_ok=True)

    quality = run_quality_checks(dataset, as_of=dataset.calendar.last,
                                 max_stale_days=int(cfg.get("data.max_stale_days", 3)))
    write_frame(out / "quality_report.csv", quality.to_frame())

    strategy_id = args.strategy or cfg.get("strategy.active", "DUAL_MOMENTUM_V1")
    risk_engine = _build_risk_engine(cfg)
    log.info("开始回测 %s ...", strategy_id)
    result = _backtest(cfg, dataset, strategy_id, risk_engine)
    log.info("回测完成：%s", result.metrics.get("cagr"))

    write_json(out / "metrics.json", result.metrics)
    write_json(out / "backtest_config.json", result.config)
    nav_df = result.nav.reset_index()
    nav_df.columns = ["trade_date", "nav"]
    if result.benchmark is not None:
        bench_df = result.benchmark.reset_index()
        bench_df.columns = ["trade_date", "benchmark"]
        nav_df = nav_df.merge(bench_df, on="trade_date", how="left")
    write_frame(out / "nav.csv", nav_df)
    write_frame(out / "trades.csv", result.fills)
    write_frame(out / "rejected_orders.csv", result.rejected)
    write_json(out / "target_history.json", result.targets)

    batch, risk, latest_quality, stress = _latest_decision(cfg, dataset, strategy_id, risk_engine, result)
    write_frame(out / "decision_cards.csv", batch.to_frame())
    write_json(out / "decision_batch.json", batch.to_dict())
    write_json(out / "risk_snapshot.json", risk.assessment.to_dict())
    write_json(out / "stress_tests.json", [s.to_dict() for s in stress])
    write_json(out / "quality_report.json", quality.summary())
    (out / "decision_report.md").write_text(
        decision_markdown(batch, result.metrics, quality.summary(), stress), encoding="utf-8"
    )

    registry = StrategyRegistry(cfg.strategies)
    manifest = {
        "project": cfg.get("project.name"),
        "version": cfg.get("project.version"),
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "data_source": dataset.source,
        "data_start": str(dataset.bars["trade_date"].min().date()),
        "data_end": str(dataset.bars["trade_date"].max().date()),
        "strategy_id": strategy_id,
        "strategy": registry.describe(strategy_id),
        "strategy_snapshot": StrategyRegistrySnapshot([registry.describe(n) for n in registry.names()]).to_dict(),
        "risk_policy_version": cfg.risk_policy.get("version"),
        "config_digest": _config_digest(cfg),
        "metrics_digest": {k: v for k, v in result.metrics.items()},
    }
    write_json(out / "run_manifest.json", manifest)

    audit = AuditLog(out / "audit_log.jsonl")
    audit.append("run_demo", {
        "strategy_id": strategy_id, "as_of": batch.as_of.isoformat(),
        "risk_state": batch.risk_state, "approval_verified": batch.approval_verified,
        "decision_cards": len(batch.cards), "config_digest": manifest["config_digest"],
    })
    audit.append("decision_batch", batch.to_dict())
    log.info("演示产物已写入 %s（审计链校验：%s）", out, audit.verify())
    print(f"完成：{out}")
    return 0


def _config_digest(cfg: ProjectConfig) -> str:
    import hashlib
    payload = json.dumps({"system": cfg.system, "risk": cfg.risk_policy,
                          "strategies": cfg.strategies}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def cmd_backtest(cfg: ProjectConfig, args: argparse.Namespace) -> int:
    dataset = _load_dataset(cfg)
    result = _backtest(cfg, dataset, args.strategy or cfg.get("strategy.active"), _build_risk_engine(cfg))
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str))
    if args.out:
        out = cfg.resolve(args.out)
        write_json(out / "metrics.json", result.metrics)
        write_frame(out / "trades.csv", result.fills)
    return 0


def cmd_validate(cfg: ProjectConfig, args: argparse.Namespace) -> int:
    """样本内/样本外 + 参数网格 CSCV PBO。"""
    dataset = _load_dataset(cfg)
    strategy_id = args.strategy or cfg.get("strategy.active")
    base_cfg = StrategyRegistry(cfg.strategies).config(strategy_id)
    base_params = dict(base_cfg.get("params", {}))

    result = _backtest(cfg, dataset, strategy_id, _build_risk_engine(cfg))
    is_nav, oos_nav = split_is_oos(result.nav, is_ratio=0.7)
    report = {
        "strategy_id": strategy_id,
        "is": summarize(is_nav, None, None, float(cfg.get("market.risk_free_rate", 0.015))),
        "oos": summarize(oos_nav, None, None, float(cfg.get("market.risk_free_rate", 0.015))),
        "full": result.metrics,
    }

    # 参数网格：仅在动量类策略上做，用真实回测收益构造 PBO 输入
    trials: list[pd.Series] = []
    trial_params: list[dict] = []
    if base_cfg.get("class") in {"dual_momentum", "etf_rotation"}:
        for lookback in (126, 189, 252):
            for top_n in (3, 4, 5):
                params = {**base_params, "lookback_days": lookback, "top_n": top_n}
                variant_cfg = json.loads(json.dumps(cfg.strategies))
                variant_cfg["strategies"][strategy_id]["params"] = params
                variant_strategy = StrategyRegistry(variant_cfg).build(strategy_id)
                variant = Backtester(
                    dataset=dataset, strategy=variant_strategy,
                    config=BacktestConfig(
                        start=_as_date(cfg.get("backtest.start", "2021-01-04"), "2021-01-04"),
                        end=_as_date(cfg.get("backtest.end", "2026-06-30"), "2026-06-30"),
                        initial_cash=float(cfg.get("portfolio.initial_cash", 1_000_000)),
                        rebalance_frequency=str(cfg.get("portfolio.rebalance_frequency", "monthly")),
                        risk_free_rate=float(cfg.get("market.risk_free_rate", 0.015)),
                    ),
                    cost_model=CostModel.from_config(cfg.system.get("costs", {})),
                    rulebook=TradingRuleBook.with_2026_revision(),
                    risk_engine=_build_risk_engine(cfg),
                ).run()
                trials.append(
                    variant.nav.pct_change(fill_method=None).dropna()
                    .rename(f"{strategy_id}_trial_{len(trials) + 1}")
                )
                trial_params.append(params)
                print(f"[validate] trial {len(trials)}: lookback={lookback} top_n={top_n}", flush=True)

    pbo = cscv_pbo(pd.concat(trials, axis=1), partitions=int(cfg.get("backtest.pbo.partitions", 10))) if trials else None
    report["pbo"] = pbo.to_dict() if pbo else None
    report["trials"] = trial_params
    out = cfg.resolve(args.out or "reports/validation")
    write_json(out / f"{strategy_id}_validation.json", report)
    print(json.dumps({k: report[k] for k in ("strategy_id", "is", "oos", "pbo")},
                     ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_serve(cfg: ProjectConfig, args: argparse.Namespace) -> int:
    import subprocess
    app = Path(__file__).parent / "ui" / "app.py"
    return subprocess.call([sys.executable, "-m", "streamlit", "run", str(app)])  # pragma: no cover


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wealth-quant", description="中国市场稳健理财量化决策系统")
    parser.add_argument("--root", default=None, help="项目根目录")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("generate-sample", help="生成离线示例数据")
    p.set_defaults(func=cmd_generate_sample)

    p = sub.add_parser("demo", help="运行完整演示（回测 + 风控 + 决策报告）")
    p.add_argument("--strategy", default=None)
    p.add_argument("--out", default="reports/demo")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("backtest", help="运行单策略回测")
    p.add_argument("--strategy", default=None)
    p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("validate", help="样本内/外验证 + PBO")
    p.add_argument("--strategy", default=None)
    p.add_argument("--out", default="reports/validation")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("serve", help="启动 Streamlit 决策台")
    p.set_defaults(func=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = ProjectConfig.load(args.root)
    return int(args.func(cfg, args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
