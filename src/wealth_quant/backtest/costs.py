"""交易成本模型：佣金、印花税、过户费、滑点与市场冲击。

A 股要点：
- 佣金双边收取，通常有最低 5 元；
- 印花税仅股票卖出收取（当前 0.05%，以最新政策为准）；
- 过户费沪深双边收取；
- ETF / 债券基金免征印花税；
- 滑点与冲击成本随订单额/日均成交额上升。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    commission_rate: float = 0.00025
    commission_min: float = 5.0
    stamp_duty_rate: float = 0.0005
    transfer_fee_rate: float = 0.00001
    slippage_bps: float = 8.0
    market_impact_coef: float = 0.10
    max_participation_rate: float = 0.10

    @classmethod
    def from_config(cls, costs: dict) -> "CostModel":
        return cls(
            commission_rate=float(costs.get("commission_rate", 0.00025)),
            commission_min=float(costs.get("commission_min", 5.0)),
            stamp_duty_rate=float(costs.get("stamp_duty_rate", 0.0005)),
            transfer_fee_rate=float(costs.get("transfer_fee_rate", 0.00001)),
            slippage_bps=float(costs.get("slippage_bps", 8.0)),
            market_impact_coef=float(costs.get("market_impact_coef", 0.10)),
            max_participation_rate=float(costs.get("max_participation_rate", 0.10)),
        )

    def commission(self, notional: float) -> float:
        return max(self.commission_min, abs(notional) * self.commission_rate)

    def stamp_duty(self, notional: float, is_stock: bool) -> float:
        return abs(notional) * self.stamp_duty_rate if is_stock else 0.0

    def transfer_fee(self, notional: float) -> float:
        return abs(notional) * self.transfer_fee_rate

    def slippage_cost(self, notional: float) -> float:
        return abs(notional) * self.slippage_bps / 10000.0

    def market_impact(self, notional: float, adv: float) -> float:
        """平方根冲击模型：cost_rate = coef * sqrt(participation)。"""
        if adv is None or adv <= 0 or self.market_impact_coef <= 0:
            return 0.0
        participation = min(abs(notional) / adv, self.max_participation_rate)
        return abs(notional) * self.market_impact_coef * (participation ** 0.5)

    def total_cost(self, notional: float, side: str, is_stock: bool, adv: float | None = None) -> dict[str, float]:
        commission = self.commission(notional)
        stamp = self.stamp_duty(notional, is_stock) if side == "SELL" else 0.0
        transfer = self.transfer_fee(notional)
        slippage = self.slippage_cost(notional)
        impact = self.market_impact(notional, adv or 0.0)
        return {
            "commission": commission,
            "stamp_duty": stamp,
            "transfer_fee": transfer,
            "slippage": slippage,
            "market_impact": impact,
            "total": commission + stamp + transfer + slippage + impact,
        }

    def execution_price(self, side: str, reference_price: float, notional: float, adv: float | None = None) -> float:
        """在参考价上加入滑点与冲击，买入上浮、卖出下调。"""
        if reference_price <= 0:
            return reference_price
        slip = self.slippage_bps / 10000.0
        impact_rate = 0.0
        if adv and adv > 0:
            participation = min(abs(notional) / adv, self.max_participation_rate)
            impact_rate = self.market_impact_coef * (participation ** 0.5)
        total = slip + impact_rate
        return reference_price * (1 + total) if side == "BUY" else reference_price * (1 - total)

    def max_tradable_shares(self, adv: float | None, price: float) -> float | None:
        if not adv or adv <= 0 or price <= 0:
            return None
        return adv * self.max_participation_rate / price
