"""回测引擎：next-open 成交（信号日之后才成交）、涨跌停表、净值长度。"""

from __future__ import annotations

from datetime import date

import pandas as pd

from wealth_quant.backtest.engine import BacktestConfig, Backtester
from wealth_quant.strategies.base import TargetPortfolio


class _BuyHalf:
    strategy_id = "TEST_BUY_HALF"
    strategy_name = "测试买入"
    version = "1.0.0"

    def generate(self, ctx):
        return TargetPortfolio(
            as_of=ctx.as_of, strategy_id=self.strategy_id, strategy_name=self.strategy_name,
            version=self.version, weights=pd.Series({"600519.SH": 0.50}),
            rationale={"600519.SH": ["测试用固定目标"]},
        )


def test_next_open_means_fills_after_signal_date(small_dataset):
    cfg = BacktestConfig(
        start=date(2023, 1, 1), end=date(2023, 12, 31), initial_cash=1_000_000,
        rebalance_frequency="monthly", execution="next_open",
    )
    result = Backtester(small_dataset, _BuyHalf(), cfg).run()
    assert not result.nav.empty
    assert len(result.nav) == len(small_dataset.calendar.range(cfg.start, cfg.end))
    assert result.targets, "至少应有一次调仓目标"
    if not result.fills.empty:
        first_signal = pd.Timestamp(result.targets[0]["date"])
        trade_fills = result.fills[result.fills["side"].isin(["BUY", "SELL"])]
        assert not trade_fills.empty
        assert trade_fills["date"].min() > first_signal, "信号日收盘生成，下一交易日开盘成交"
        assert (trade_fills["total_cost"] >= 0).all()


def test_limit_table_uses_rulebook(small_dataset):
    cfg = BacktestConfig(start=date(2023, 1, 1), end=date(2023, 12, 31))
    bt = Backtester(small_dataset, _BuyHalf(), cfg)
    limits = bt._build_limit_table()
    assert "600519.SH" in limits.columns
    assert float(limits["600519.SH"].dropna().iloc[0]) == 0.10


def test_limit_up_rejects_buy_order(small_dataset):
    from dataclasses import replace

    bars = small_dataset.bars.copy()
    prev_day = pd.Timestamp("2023-01-31")
    exec_day = pd.Timestamp("2023-02-01")
    dates = pd.to_datetime(bars["trade_date"])
    prev_mask = (bars["instrument_id"] == "600519.SH") & (dates == prev_day)
    prev_close = float(bars.loc[prev_mask, "close"].iloc[0])
    # 把执行日开盘价打到涨停之上，模拟“一字板买不到”
    mask = (bars["instrument_id"] == "600519.SH") & (dates == exec_day)
    limit_up_open = round(prev_close * 1.12, 2)
    bars.loc[mask, ["open", "high", "low", "close"]] = limit_up_open
    bars.loc[mask, "volume"] = 1_000_000
    dataset = replace(small_dataset, bars=bars)

    cfg = BacktestConfig(
        start=date(2023, 1, 1), end=date(2023, 3, 31), initial_cash=1_000_000, rebalance_frequency="monthly",
    )
    result = Backtester(dataset, _BuyHalf(), cfg).run()
    assert not result.rejected.empty
    assert "limit_up" in set(result.rejected["code"]), result.rejected.to_dict("records")
