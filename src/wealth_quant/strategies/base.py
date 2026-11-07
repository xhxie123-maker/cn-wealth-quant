"""策略基类与目标组合契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

import pandas as pd

from ..data.schema import Dataset


@dataclass
class StrategyContext:
    dataset: Dataset
    as_of: date
    universe: list[str]
    benchmark: pd.Series | None = None
    risk_state: str = "GREEN"
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class TargetPortfolio:
    as_of: date
    strategy_id: str
    strategy_name: str
    version: str
    weights: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    scores: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    rationale: dict[str, list[str]] = field(default_factory=dict)
    invalidation: dict[str, list[str]] = field(default_factory=dict)
    horizon: str = "中期(1-12个月)"
    diagnostics: dict[str, Any] = field(default_factory=dict)
    cash_weight: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.weights, pd.Series):
            self.weights = pd.Series(self.weights, dtype=float)
        self.weights = self.weights.astype(float).replace([float("inf"), float("-inf")], 0.0).fillna(0.0)
        self.weights = self.weights[self.weights > 1e-8].sort_values(ascending=False)
        total = float(self.weights.sum())
        if total > 1.0 + 1e-6:
            raise ValueError(f"目标权重之和超过 100%: {total:.4f}")
        self.cash_weight = float(max(0.0, 1.0 - total))
        if not isinstance(self.scores, pd.Series):
            self.scores = pd.Series(self.scores, dtype=float)

    def to_frame(self) -> pd.DataFrame:
        rows = []
        for iid, weight in self.weights.items():
            rows.append({
                "instrument_id": iid,
                "target_weight": float(weight),
                "score": float(self.scores.get(iid, float("nan"))),
                "rationale": "；".join(self.rationale.get(iid, [])),
            })
        df = pd.DataFrame(rows)
        if not df.empty:
            df = df.sort_values("target_weight", ascending=False).reset_index(drop=True)
        return df


class Strategy(Protocol):
    strategy_id: str
    strategy_name: str
    version: str

    def generate(self, ctx: StrategyContext) -> TargetPortfolio: ...
