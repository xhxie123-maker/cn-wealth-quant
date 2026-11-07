"""价值 + 质量复合策略（含低波动约束）。

财务数据严格使用 point-in-time：只取 available_at <= 决策时点的最近一期，
避免把后来才公布的财报提前用于历史回测。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ..factors.compute import build_factor_snapshot, composite_group, investable_mask
from ..portfolio.optimizer import project_weights, risk_parity, shrink_cov
from .base import StrategyContext, TargetPortfolio

VALUE_WEIGHTS = {"ep": 0.30, "bp": 0.25, "sp": 0.15, "dividend_yield": 0.15, "fcf_yield": 0.15}
QUALITY_WEIGHTS = {
    "roe": 0.25, "roa": 0.15, "profit_growth": 0.20, "ocf_to_profit": 0.15,
    "debt_ratio": 0.15, "earnings_stability": 0.10,
}


class ValueQualityStrategy:
    def __init__(self, strategy_id: str, name: str, version: str, params: dict[str, Any]):
        self.strategy_id = strategy_id
        self.strategy_name = name
        self.version = version
        self.params = params

    def generate(self, ctx: StrategyContext) -> TargetPortfolio:
        top_n = int(self.params.get("top_n", 4))
        min_coverage = float(self.params.get("min_coverage", 0.6))
        weights_cfg = self.params.get("weights", {"value": 0.4, "quality": 0.4, "low_vol": 0.2})
        max_weight = float(self.params.get("max_weight", 0.30))

        snap, cov = build_factor_snapshot(ctx.dataset, ctx.universe, ctx.as_of, benchmark=ctx.benchmark)
        if snap.meta.empty or "value" not in snap.groups:
            return TargetPortfolio(
                as_of=ctx.as_of, strategy_id=self.strategy_id, strategy_name=self.strategy_name,
                version=self.version, diagnostics={"reason": "无可用财务数据（ETF/债券不适用基本面策略）"},
                invalidation={"*": ["投资域内没有可用 point-in-time 财务数据"]},
            )

        industry = snap.meta["industry"] if "industry" in snap.meta else None

        def safe_composite(group: str, weights: dict[str, float], coverage: float):
            """数据尚未积累够（如上市不足 120 日）时跳过该因子组，而不是让整个策略报错。"""
            try:
                return composite_group(snap, group, weights, coverage, industry)
            except ValueError:
                return None

        parts = {
            "value": safe_composite("value", VALUE_WEIGHTS, min_coverage),
            "quality": safe_composite("quality", QUALITY_WEIGHTS, min_coverage),
            "low_vol": safe_composite("risk", {"vol_120": 0.6, "downside_vol": 0.4}, 0.5),
        }
        available = {k: v for k, v in parts.items() if v is not None}
        if not available:
            return TargetPortfolio(
                as_of=ctx.as_of, strategy_id=self.strategy_id, strategy_name=self.strategy_name,
                version=self.version,
                diagnostics={"reason": "因子数据尚未达到最小历史长度，本期不建仓"},
                invalidation={"*": ["有效因子覆盖率不足"]},
            )

        total_weight = sum(float(weights_cfg.get(k, 0.0)) for k in available) or 1.0
        total = sum(
            float(weights_cfg.get(k, 0.0)) / total_weight * part.score
            for k, part in available.items()
        )

        mask = investable_mask(snap, min_amount=float(ctx.params.get("min_avg_amount_cny", 0.0) or 0.0))
        total = total.where(mask.reindex(total.index).fillna(False))
        total = total.dropna().sort_values(ascending=False)
        selected = total.head(top_n).index.tolist()

        if not selected:
            return TargetPortfolio(
                as_of=ctx.as_of, strategy_id=self.strategy_id, strategy_name=self.strategy_name,
                version=self.version, weights=pd.Series(dtype=float), scores=total,
                diagnostics={"reason": "无资产满足价值/质量/流动性门槛"},
                invalidation={"*": ["全部资产评分无效或流动性不足"]},
            )

        sub_cov = cov.reindex(index=selected, columns=selected).dropna(how="all") if not cov.empty else pd.DataFrame()
        if sub_cov.empty or len(sub_cov) < 2:
            weights = pd.Series(1.0 / len(selected), index=selected)
        else:
            weights = risk_parity(shrink_cov(sub_cov, 0.25))
        weights = project_weights(weights, max_weight=max_weight)

        rationale: dict[str, list[str]] = {}
        invalidation: dict[str, list[str]] = {}
        for iid in selected:
            reasons = [f"综合评分 {total[iid]:+.2f}，排名 {selected.index(iid) + 1}/{len(total)}"]
            for group, factor_score in available.items():
                label = {"value": "估值", "quality": "质量", "low_vol": "低波动"}.get(group, group)
                top = factor_score.top_contributors(iid, n=2)
                if top:
                    detail = "、".join(f"{name} 贡献 {value:+.2f}" for name, value in top)
                    reasons.append(f"{label}分项 {factor_score.score.get(iid, float('nan')):+.2f}（{detail}）")
            vol = snap.groups["risk"].get("vol_120", pd.Series(dtype=float)).get(iid, np.nan)
            if pd.notna(vol):
                reasons.append(f"120 日年化波动 {vol:.1%}，用于低波动约束与风险平价")
            reasons.append(f"按风险平价分配目标权重 {weights.get(iid, 0):.1%}")
            rationale[iid] = reasons
            invalidation[iid] = [
                "下一期财报 ROE 或经营现金流显著恶化（低于行业中位数）",
                "估值分位升至近三年 80% 以上，或综合评分跌出前 50%",
                "收盘价跌破 MA200，或财务数据停更超过一个季度",
            ]

        return TargetPortfolio(
            as_of=ctx.as_of, strategy_id=self.strategy_id, strategy_name=self.strategy_name,
            version=self.version, weights=weights, scores=total,
            rationale=rationale, invalidation=invalidation,
            horizon=ctx.params.get("horizon", "中期(1-12个月)"),
            diagnostics={
                "value_rank": (available["value"].score.sort_values(ascending=False).head(10).to_dict()
                               if "value" in available else {}),
                "quality_rank": (available["quality"].score.sort_values(ascending=False).head(10).to_dict()
                                 if "quality" in available else {}),
                "universe_scored": int(total.shape[0]),
            },
            metadata={"top_n": top_n, "weights": weights_cfg, "min_coverage": min_coverage},
        )
