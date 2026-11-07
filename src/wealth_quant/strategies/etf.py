"""ETF 动量轮动策略：只在 ETF / 债券 ETF / 货币 ETF / 黄金 ETF 池内轮动。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..data.schema import Dataset
from .base import StrategyContext, TargetPortfolio
from .momentum import DualMomentumStrategy

ETF_TYPES = {"etf", "bond_etf", "money_etf", "gold_etf", "crossborder_etf"}


class ETFRotationStrategy:
    def __init__(self, strategy_id: str, name: str, version: str, params: dict[str, Any]):
        self.strategy_id = strategy_id
        self.strategy_name = name
        self.version = version
        self.params = params

    def generate(self, ctx: StrategyContext) -> TargetPortfolio:
        inst = ctx.dataset.instruments
        etf_ids = [i for i in ctx.universe if i in inst.index and inst.loc[i, "asset_type"] in ETF_TYPES]
        if not etf_ids:
            return TargetPortfolio(
                as_of=ctx.as_of, strategy_id=self.strategy_id, strategy_name=self.strategy_name,
                version=self.version, diagnostics={"reason": "投资域内没有 ETF 类资产"},
                invalidation={"*": ["投资域内没有 ETF 类资产"]},
            )

        filtered_dataset = Dataset(
            instruments=ctx.dataset.instruments.loc[etf_ids],
            bars=ctx.dataset.bars[ctx.dataset.bars["instrument_id"].isin(etf_ids)].reset_index(drop=True),
            calendar=ctx.dataset.calendar,
            fundamentals=ctx.dataset.fundamentals,
            corporate_actions=ctx.dataset.corporate_actions,
            macro=ctx.dataset.macro,
            benchmark=ctx.dataset.benchmark,
            source=ctx.dataset.source,
        )
        sub_ctx = replace(ctx, dataset=filtered_dataset, universe=etf_ids)
        delegate = DualMomentumStrategy(self.strategy_id, self.strategy_name, self.version, {
            **self.params,
            "max_weight": self.params.get("max_weight", 0.40),
        })
        target = delegate.generate(sub_ctx)
        target.metadata["universe_type"] = "ETF_ONLY"
        target.horizon = self.params.get("horizon", "中期(1-12个月)")
        return target
