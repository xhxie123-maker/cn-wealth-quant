"""双动量趋势策略（建设手册 4.3 的工程基准策略）。

逻辑：
1. 12-1 动量：过去 252 个交易日收益，跳过最近 21 个交易日（规避短期反转）；
2. 长期均线过滤：收盘价高于 MA200，剔除明显下跌趋势；
3. 绝对动量门槛：动量必须为正；
4. 入选资产按风险平价分配风险预算，约束单资产上限；
5. 无资产通过过滤时保持现金，不强行满仓。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from ..factors.compute import build_factor_snapshot, investable_mask
from ..portfolio.optimizer import project_weights, risk_parity, shrink_cov
from .base import StrategyContext, TargetPortfolio


class DualMomentumStrategy:
    def __init__(self, strategy_id: str, name: str, version: str, params: dict[str, Any]):
        self.strategy_id = strategy_id
        self.strategy_name = name
        self.version = version
        self.params = params

    def generate(self, ctx: StrategyContext) -> TargetPortfolio:
        lookback = int(self.params.get("lookback_days", 252))
        skip = int(self.params.get("skip_days", 21))
        top_n = int(self.params.get("top_n", 4))
        ma_window = int(self.params.get("ma_filter_days", 200))
        min_mom = float(self.params.get("min_momentum", 0.0))
        max_weight = float(self.params.get("max_weight", 0.30))

        panel = ctx.dataset.price_panel("close", adjusted=True).ffill()
        panel = panel[[i for i in ctx.universe if i in panel.columns]]
        diagnostics: dict[str, Any] = {"eligible": [], "filtered": [], "panel_rows": len(panel)}

        if len(panel) < max(lookback + skip + 1, ma_window + 1) or panel.empty:
            return TargetPortfolio(
                as_of=ctx.as_of, strategy_id=self.strategy_id, strategy_name=self.strategy_name,
                version=self.version, weights=pd.Series(dtype=float), scores=pd.Series(dtype=float),
                rationale={}, diagnostics={**diagnostics, "reason": "历史数据不足"},
                invalidation={"*": ["历史数据长度不足，无法计算 12-1 动量"]},
                horizon=ctx.params.get("horizon", "中期(1-12个月)"),
            )

        close = panel.iloc[-1]
        mom = panel.iloc[-1 - skip] / panel.iloc[-1 - skip - lookback] - 1
        ma = panel.tail(ma_window).mean()
        above_ma = close > ma
        mom = mom.where(np.isfinite(mom))
        passed = (mom > min_mom) & above_ma.fillna(False)

        # ST / 流动性过滤
        snap, cov = build_factor_snapshot(ctx.dataset, ctx.universe, ctx.as_of, include_fundamentals=False)
        mask = investable_mask(snap, min_amount=0.0)
        passed = passed & mask.reindex(passed.index).fillna(False)

        ranked = mom[passed].sort_values(ascending=False)
        selected = ranked.head(top_n).index.tolist()
        diagnostics["eligible"] = selected
        diagnostics["filtered"] = [i for i in ctx.universe if i not in selected]
        diagnostics["mom_12_1"] = {i: float(mom.get(i, np.nan)) for i in ctx.universe}

        if not selected:
            return TargetPortfolio(
                as_of=ctx.as_of, strategy_id=self.strategy_id, strategy_name=self.strategy_name,
                version=self.version, weights=pd.Series(dtype=float), scores=ranked,
                rationale={}, diagnostics={**diagnostics, "reason": "无资产通过趋势与绝对动量过滤"},
                invalidation={"*": ["全部资产跌破 MA200 或动量为负，策略建议保持现金"]},
                horizon=ctx.params.get("horizon", "中期(1-12个月)"),
            )

        sub_cov = cov.reindex(index=selected, columns=selected).dropna(how="all").dropna(axis=1, how="all") if not cov.empty else pd.DataFrame()
        if sub_cov.empty or len(sub_cov) < len(selected):
            weights = pd.Series(1.0 / len(selected), index=selected)
        else:
            weights = risk_parity(shrink_cov(sub_cov, 0.2))
        weights = project_weights(weights, max_weight=max_weight)

        rationale: dict[str, list[str]] = {}
        invalidation: dict[str, list[str]] = {}
        for iid in selected:
            rank = selected.index(iid) + 1
            rationale[iid] = [
                f"12-1 动量为 {ranked[iid]:.1%}，在通过过滤的资产中排名 {rank}/{(passed).sum()}",
                f"收盘价 {close[iid]:.2f} 高于 MA{ma_window}（{ma[iid]:.2f}），趋势向上",
                f"按风险平价分配策略目标权重 {weights.get(iid, 0):.1%}（最终权重需经风控裁剪）",
            ]
            invalidation[iid] = [
                f"收盘价跌破 MA{ma_window}（当前 {ma[iid]:.2f}）",
                f"12-1 动量转负或跌出前 {top_n} 名",
                "风控状态转为 RED 或数据超过 3 个交易日未更新",
            ]

        return TargetPortfolio(
            as_of=ctx.as_of,
            strategy_id=self.strategy_id,
            strategy_name=self.strategy_name,
            version=self.version,
            weights=weights,
            scores=ranked,
            rationale=rationale,
            invalidation=invalidation,
            horizon=ctx.params.get("horizon", "中期(1-12个月)"),
            diagnostics=diagnostics,
            metadata={
                "lookback_days": lookback, "skip_days": skip, "top_n": top_n,
                "ma_filter_days": ma_window, "min_momentum": min_mom,
            },
        )

    @staticmethod
    def _fmt_pct(x: float) -> str:
        return f"{x:.1%}" if pd.notna(x) else "—"
