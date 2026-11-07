"""决策中心：把“策略目标 + 风控审批”转换成可读、可审核的投资建议卡。

每张卡片必须回答三个问题：
1. 为什么现在产生这个信号？
2. 这个建议可能有什么风险？
3. 什么情况下需要撤销或重新评估？

系统只提供建议，必须人工确认；不生成可直接发送给券商的订单。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Mapping

import numpy as np
import pandas as pd

from ..risk.limits import RiskApproval, RiskDecision, RiskEngine
from ..strategies.base import TargetPortfolio

SIGNAL_BUY = "买入"
SIGNAL_ADD = "加仓"
SIGNAL_HOLD = "持有"
SIGNAL_REDUCE = "减仓"
SIGNAL_SELL = "卖出"
SIGNAL_WATCH = "观望"

FIELD_LABELS = {
    "instrument_id": "资产代码",
    "name": "资产名称",
    "signal": "当前信号",
    "strength": "信号强度",
    "current_weight": "当前仓位",
    "target_weight": "目标仓位",
    "delta_weight": "建议调整比例",
    "suggested_shares": "建议数量(股/份)",
    "suggested_amount": "建议金额(元)",
    "horizon": "投资周期",
    "risk_level": "风险等级",
    "rationale": "决策理由",
    "invalidation": "失效条件",
}


class RiskApprovalError(RuntimeError):
    """决策引擎未获得合法风控审批时抛出。"""


@dataclass
class DecisionCard:
    instrument_id: str
    name: str
    signal: str
    strength: float
    current_weight: float
    target_weight: float
    delta_weight: float
    suggested_shares: float
    suggested_amount: float
    horizon: str
    risk_level: str
    rationale: list[str]
    invalidation: list[str]
    price: float = float("nan")
    score: float = float("nan")
    as_of: str = ""
    strategy: str = ""
    lot_size: int = 100
    risk_state: str = "GREEN"

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "name": self.name,
            "signal": self.signal,
            "strength": self.strength,
            "current_weight": self.current_weight,
            "target_weight": self.target_weight,
            "delta_weight": self.delta_weight,
            "suggested_shares": self.suggested_shares,
            "suggested_amount": self.suggested_amount,
            "horizon": self.horizon,
            "risk_level": self.risk_level,
            "rationale": "；".join(self.rationale),
            "invalidation": "；".join(self.invalidation),
            "price": self.price,
            "score": self.score,
            "as_of": self.as_of,
            "strategy": self.strategy,
        }

    def to_row(self) -> dict[str, Any]:
        data = self.to_dict()
        return {FIELD_LABELS.get(k, k): v for k, v in data.items() if k in FIELD_LABELS}


@dataclass
class DecisionBatch:
    as_of: date
    strategy_id: str
    strategy_name: str
    strategy_version: str
    risk_state: str
    cards: list[DecisionCard] = field(default_factory=list)
    approval_verified: bool = False
    risk_reasons: list[str] = field(default_factory=list)
    generated_at: str = ""
    manual_confirmation_required: bool = True
    auto_execution: bool = False
    disclaimers: list[str] = field(default_factory=lambda: [
        "本报告由量化模型自动生成，仅用于辅助决策，不构成投资建议。",
        "所有信号均需人工复核后由投资者自行决定是否交易，系统不具备下单权限。",
        "历史回测与模拟结果不代表未来收益，市场存在本金损失风险。",
    ])

    def to_frame(self) -> pd.DataFrame:
        if not self.cards:
            return pd.DataFrame(columns=list(FIELD_LABELS.values()))
        return pd.DataFrame([c.to_row() for c in self.cards])

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "strategy": {
                "id": self.strategy_id, "name": self.strategy_name, "version": self.strategy_version,
            },
            "risk_state": self.risk_state,
            "approval_verified": self.approval_verified,
            "risk_reasons": self.risk_reasons,
            "generated_at": self.generated_at,
            "manual_confirmation_required": self.manual_confirmation_required,
            "auto_execution": self.auto_execution,
            "disclaimers": self.disclaimers,
            "cards": [c.to_dict() for c in self.cards],
        }


class DecisionEngine:
    def __init__(self, risk_engine: RiskEngine, min_weight_change: float = 0.02, max_cards: int = 20,
                 require_risk_approval: bool = True):
        self.risk_engine = risk_engine
        self.min_weight_change = float(min_weight_change)
        self.max_cards = int(max_cards)
        self.require_risk_approval = bool(require_risk_approval)

    def _signal(self, current: float, target: float) -> str:
        delta = target - current
        if target <= 1e-9 and current <= 1e-9:
            return SIGNAL_WATCH
        if target <= 1e-9 < current:
            return SIGNAL_SELL
        if target > 1e-9 and current <= 1e-9:
            return SIGNAL_BUY
        if delta > self.min_weight_change:
            return SIGNAL_ADD
        if delta < -self.min_weight_change:
            return SIGNAL_REDUCE
        return SIGNAL_HOLD

    def build(
        self,
        as_of: date,
        target: TargetPortfolio,
        risk: RiskDecision,
        current_weights: pd.Series,
        nav: float,
        prices: Mapping[str, float],
        names: Mapping[str, str],
        lot_sizes: Mapping[str, int],
        metadata: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> DecisionBatch:
        approved = risk.approved_weights if risk.approved_weights is not None else pd.Series(dtype=float)
        approval_ok = False

        if risk.approval is not None:
            if not self.risk_engine.verify_approval(risk.approval, as_of, target.strategy_id, approved):
                raise RiskApprovalError("风控审批令牌校验失败：可能被伪造或权重已被篡改")
            approval_ok = True
        elif self.require_risk_approval and not risk.rejected:
            raise RiskApprovalError("缺少风控审批令牌，拒绝生成可执行建议")

        current = current_weights.astype(float)
        universe = sorted(set(approved.index) | set(current[current > 1e-9].index))
        scores = target.scores
        risk_label = risk.assessment.state

        cards: list[DecisionCard] = []
        for iid in universe:
            cur_w = float(current.get(iid, 0.0))
            tgt_w = float(approved.get(iid, 0.0)) if len(approved) else 0.0
            price = float(prices.get(iid, np.nan))
            lot = int(lot_sizes.get(iid, 100))
            delta_w = tgt_w - cur_w
            signal = self._signal(cur_w, tgt_w) if not risk.rejected else SIGNAL_WATCH
            if risk.rejected:
                tgt_w = cur_w
                delta_w = 0.0

            shares = 0.0
            amount = 0.0
            if np.isfinite(price) and price > 0 and abs(delta_w) > 1e-9 and nav > 0 and not risk.rejected:
                shares = math.floor(abs(delta_w) * nav / price / lot) * lot
                amount = shares * price
                if shares <= 0:
                    shares = 0.0
                    amount = 0.0

            reasons = list(target.rationale.get(iid, []))
            strategy_target = float(target.weights.get(iid, 0.0)) if iid in target.weights.index else 0.0
            if iid in target.weights.index and abs(tgt_w - strategy_target) > 0.005:
                reasons.append(
                    f"风控将策略目标权重由 {strategy_target:.1%} 调整为 {tgt_w:.1%}（单资产/行业/总仓位约束）"
                )
            if iid not in target.weights.index:
                reasons.append("该资产不在本期策略目标组合内：不新增敞口，按调仓规则退出或观望")
            if risk.reasons:
                reasons.extend(risk.reasons[:2])
            if risk.rejected:
                reasons.append("风控否决：本期不产生可执行交易，维持当前仓位")
            if not reasons:
                reasons.append("信号未达到最小调仓阈值，维持现有仓位")

            invalidation = list(target.invalidation.get(iid, []))
            if not invalidation:
                invalidation = [
                    "策略评分跌出目标组合或动量/估值条件反转",
                    "风控状态转为 RED 或 SYSTEM_HALT",
                    "行情数据超过 3 个交易日未更新",
                ]

            score = float(scores.get(iid, np.nan)) if len(scores) else float("nan")
            cards.append(DecisionCard(
                instrument_id=iid,
                name=names.get(iid, iid),
                signal=signal,
                strength=score,
                current_weight=cur_w,
                target_weight=tgt_w,
                delta_weight=delta_w,
                suggested_shares=shares,
                suggested_amount=amount,
                horizon=target.horizon,
                risk_level=risk_label,
                rationale=reasons,
                invalidation=invalidation,
                price=price,
                score=score,
                as_of=as_of.isoformat(),
                strategy=f"{target.strategy_name} {target.version}",
                lot_size=lot,
                risk_state=risk_label,
            ))

        # 优先展示需要动作的卡片
        priority = {SIGNAL_SELL: 0, SIGNAL_REDUCE: 1, SIGNAL_BUY: 2, SIGNAL_ADD: 3, SIGNAL_HOLD: 4, SIGNAL_WATCH: 5}
        cards.sort(key=lambda c: (priority.get(c.signal, 9), -abs(c.delta_weight)))
        cards = cards[: self.max_cards]

        return DecisionBatch(
            as_of=as_of,
            strategy_id=target.strategy_id,
            strategy_name=target.strategy_name,
            strategy_version=target.version,
            risk_state=risk.assessment.state,
            cards=cards,
            approval_verified=approval_ok,
            risk_reasons=list(risk.reasons),
            generated_at=datetime.now().isoformat(timespec="seconds"),
        )
