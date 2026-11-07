"""策略注册表：按配置实例化，版本号与参数哈希可追溯。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .base import Strategy, StrategyContext, TargetPortfolio  # noqa: F401
from .etf import ETFRotationStrategy
from .fundamental import ValueQualityStrategy
from .lowvol import LowVolStrategy
from .momentum import DualMomentumStrategy

CLASS_MAP = {
    "dual_momentum": DualMomentumStrategy,
    "value_quality": ValueQualityStrategy,
    "low_vol": LowVolStrategy,
    "etf_rotation": ETFRotationStrategy,
}


def param_hash(params: dict[str, Any]) -> str:
    payload = json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


class StrategyRegistry:
    def __init__(self, config: dict[str, Any]):
        self._configs: dict[str, dict[str, Any]] = config.get("strategies", {})

    def names(self) -> list[str]:
        return list(self._configs.keys())

    def config(self, strategy_id: str) -> dict[str, Any]:
        if strategy_id not in self._configs:
            raise KeyError(f"未登记的策略: {strategy_id}；可用: {self.names()}")
        return self._configs[strategy_id]

    def build(self, strategy_id: str) -> Strategy:
        cfg = self.config(strategy_id)
        cls_key = cfg.get("class", "dual_momentum")
        if cls_key not in CLASS_MAP:
            raise KeyError(f"未知策略类: {cls_key}")
        return CLASS_MAP[cls_key](
            strategy_id=strategy_id,
            name=cfg.get("name", strategy_id),
            version=str(cfg.get("version", "1.0.0")),
            params=dict(cfg.get("params", {})),
        )

    def describe(self, strategy_id: str) -> dict[str, Any]:
        cfg = self.config(strategy_id)
        return {
            "strategy_id": strategy_id,
            "name": cfg.get("name", strategy_id),
            "version": cfg.get("version", "1.0.0"),
            "class": cfg.get("class"),
            "horizon": cfg.get("horizon"),
            "rebalance": cfg.get("rebalance"),
            "params_hash": param_hash(cfg.get("params", {})),
            "description": cfg.get("description", ""),
        }
