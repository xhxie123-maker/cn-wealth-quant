"""低波动核心策略：先按历史波动排序选池，再按风险平价分配。"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ..factors.compute import build_factor_snapshot, investable_mask
from ..portfolio.optimizer import project_weights, risk_parity, shrink_cov
from .base import StrategyContext, TargetPortfolio


class LowVolStrategy:
    def __init__(self, strategy_id: str, name: str, version: str, params: dict[str, Any]):
        self.strategy_id = strategy_id
        self.strategy_name = name
        self.version = version
        self.params = params

    def generate(self, ctx: StrategyContext) -> TargetPortfolio:
        lookback = int(self.params.get("lookback_days", 120))
        top_n = int(self.params.get("top_n", 5))
        max_weight = float(self.params.get("max_weight", 0.25))

        snap, cov = build_factor_snapshot(ctx.dataset, ctx.universe, ctx.as_of, include_fundamentals=False)
        if "risk" not in snap.groups or "vol_120" not in snap.groups["risk"]:
            return TargetPortfolio(
                as_of=ctx.as_of, strategy_id=self.strategy_id, strategy_name=self.strategy_name,
                version=self.version, diagnostics={"reason": "历史数据不足，无法估计波动率"},
                invalidation={"*": ["历史数据不足 120 个交易日"]},
            )

        vol = snap.groups["risk"]["vol_120"].dropna()
        if lookback != 120:
            returns = ctx.dataset.returns_panel(adjusted=True)
            vol = returns.tail(lookback).std(ddof=0) * np.sqrt(252)
        vol = vol.dropna()
        mask = investable_mask(snap, min_amount=0.0).reindex(vol.index).fillna(False)
        eligible = vol[mask].sort_values()
        selected = eligible.head(top_n).index.tolist()

        if not selected:
            return TargetPortfolio(
                as_of=ctx.as_of, strategy_id=self.strategy_id, strategy_name=self.strategy_name,
                version=self.version, weights=pd.Series(dtype=float), scores=pd.Series(dtype=float),
                diagnostics={"reason": "无满足流动性/ST 过滤的低波动资产"},
                invalidation={"*": ["投资域内无可用波动率估计"]},
            )

        sub_cov = cov.reindex(index=selected, columns=selected).dropna(how="all") if not cov.empty else pd.DataFrame()
        weights = risk_parity(shrink_cov(sub_cov, 0.25)) if len(selected) > 1 and not sub_cov.empty else pd.Series(1.0, index=selected)
        weights = project_weights(weights, max_weight=max_weight)

        rationale: dict[str, list[str]] = {}
        invalidation: dict[str, list[str]] = {}
        for iid in selected:
            rank = selected.index(iid) + 1
            rationale[iid] = [
                f"过去 {lookback} 个交易日年化波动 {eligible[iid]:.1%}，为候选池第 {rank} 低",
                f"按风险平价分配目标权重 {weights.get(iid, 0):.1%}，降低单一资产风险贡献",
            ]
            invalidation[iid] = [
                f"波动率升至候选池前 30%（当前 {eligible[iid]:.1%}）",
                "最大回撤超过风险政策阈值，或与组合其他资产相关性骤升",
                "流动性下降至日均成交额门槛以下",
            ]

        return TargetPortfolio(
            as_of=ctx.as_of, strategy_id=self.strategy_id, strategy_name=self.strategy_name,
            version=self.version, weights=weights,
            scores=(-eligible).sort_values(ascending=False),
            rationale=rationale, invalidation=invalidation,
            horizon=ctx.params.get("horizon", "中期(1-12个月)"),
            diagnostics={"vol_rank": eligible.head(10).to_dict(), "universe": len(eligible)},
            metadata={"lookback_days": lookback, "top_n": top_n},
        )
