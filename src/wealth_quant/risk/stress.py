"""压力测试：把组合暴露映射到情景冲击，输出组合层面损失估计。

情景参数是可配置的“假设”，不是预测。用于回答“如果发生极端情况，组合会损失多少”。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import pandas as pd

SCENARIOS: dict[str, dict[str, Any]] = {
    "equity_crash_2008": {
        "name": "权益系统性下跌（2008 类比）",
        "shocks": {"stock": -0.45, "etf": -0.40, "gold_etf": 0.03, "bond_etf": 0.05,
                   "money_etf": 0.0, "crossborder_etf": -0.35, "bond": 0.02},
        "description": "权益资产大幅下跌、避险资产小幅上涨，相关性显著上升",
    },
    "rate_shock_100bp": {
        "name": "利率上行 100bp",
        "shocks": {"stock": -0.08, "etf": -0.06, "bond_etf": -0.055, "money_etf": 0.0,
                   "gold_etf": -0.03, "crossborder_etf": -0.06, "bond": -0.06},
        "description": "无风险利率快速上行，债券与成长股同时承压",
    },
    "liquidity_freeze": {
        "name": "流动性枯竭",
        "shocks": {"stock": -0.18, "etf": -0.14, "bond_etf": -0.03, "money_etf": 0.0,
                   "gold_etf": -0.05, "crossborder_etf": -0.15, "bond": -0.04},
        "extra_cost": 0.012,
        "description": "买卖价差扩大、冲击成本上升，叠加价格下跌",
    },
    "rmb_depreciation": {
        "name": "人民币快速贬值",
        "shocks": {"stock": -0.05, "etf": -0.04, "bond_etf": -0.01, "money_etf": 0.0,
                   "gold_etf": 0.06, "crossborder_etf": 0.02, "bond": -0.01},
        "description": "汇率波动引发资金外流与风险偏好下降",
    },
    "high_volatility_regime": {
        "name": "高波动状态（波动率翻倍）",
        "shocks": {"stock": -0.12, "etf": -0.10, "bond_etf": -0.02, "money_etf": 0.0,
                   "gold_etf": 0.02, "crossborder_etf": -0.10, "bond": -0.02},
        "description": "波动率与相关性同时上升，止损与再平衡被动触发",
    },
}


@dataclass
class StressResult:
    scenario: str
    name: str
    portfolio_impact: float
    asset_impacts: dict[str, float]
    description: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "name": self.name,
            "portfolio_impact": self.portfolio_impact,
            "asset_impacts": self.asset_impacts,
            "description": self.description,
        }


def run_stress_tests(
    weights: pd.Series,
    asset_types: Mapping[str, str],
    scenarios: Mapping[str, dict[str, Any]] | None = None,
    include_cost: bool = True,
) -> list[StressResult]:
    scenarios = scenarios or SCENARIOS
    results: list[StressResult] = []
    for key, cfg in scenarios.items():
        shocks = cfg["shocks"]
        impact = 0.0
        by_asset: dict[str, float] = {}
        for iid, weight in weights.items():
            shock = float(shocks.get(asset_types.get(iid, "stock"), -0.20))
            contribution = float(weight) * shock
            by_asset[iid] = contribution
            impact += contribution
        if include_cost and "extra_cost" in cfg:
            impact -= float(cfg["extra_cost"]) * float(weights.sum())
        results.append(StressResult(
            scenario=key,
            name=cfg.get("name", key),
            portfolio_impact=float(impact),
            asset_impacts=by_asset,
            description=cfg.get("description", ""),
        ))
    return results
