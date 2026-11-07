"""成本模型与决策卡：最低佣金、印花税、滑点方向、审批缺失必须报错。"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from wealth_quant.backtest.costs import CostModel
from wealth_quant.decision.engine import DecisionEngine, RiskApprovalError
from wealth_quant.risk.limits import GREEN, RiskAssessment, RiskDecision
from wealth_quant.strategies.base import TargetPortfolio


def test_commission_minimum_and_stamp_duty():
    model = CostModel()
    assert model.commission(10_000) == pytest.approx(5.0)
    assert model.commission(100_000) == pytest.approx(25.0)
    assert model.stamp_duty(100_000, is_stock=True) == pytest.approx(50.0)
    assert model.stamp_duty(100_000, is_stock=False) == pytest.approx(0.0)


def test_execution_price_direction_and_impact():
    model = CostModel(slippage_bps=8, market_impact_coef=0.10, max_participation_rate=0.10)
    buy = model.execution_price("BUY", 10.0, 1_000_000, adv=10_000_000)
    sell = model.execution_price("SELL", 10.0, 1_000_000, adv=10_000_000)
    assert buy > 10.0
    assert sell < 10.0
    assert buy - 10.0 == pytest.approx(10.0 - sell)


def test_max_tradable_shares_respects_participation():
    model = CostModel(max_participation_rate=0.10)
    assert model.max_tradable_shares(10_000_000, 10.0) == pytest.approx(100_000)


def test_decision_engine_requires_risk_approval(cfg):
    from wealth_quant.risk.limits import RiskEngine

    risk_engine = RiskEngine(cfg.risk_policy, secret=b"decision-test")
    target = TargetPortfolio(
        as_of=date(2025, 1, 2), strategy_id="TEST", strategy_name="测试策略", version="1.0.0",
        weights=pd.Series({"A": 0.15}), rationale={"A": ["测试理由"]},
    )
    assessment = RiskAssessment(as_of=date(2025, 1, 2), state=GREEN)
    decision = RiskDecision(
        assessment=assessment, approved_weights=pd.Series({"A": 0.15}),
        requested_weights=pd.Series({"A": 0.15}), rejected=False, reasons=[], approval=None,
    )
    engine = DecisionEngine(risk_engine, require_risk_approval=True)
    with pytest.raises(RiskApprovalError):
        engine.build(
            as_of=date(2025, 1, 2), target=target, risk=decision,
            current_weights=pd.Series(dtype=float), nav=1_000_000,
            prices={"A": 10.0}, names={"A": "测试资产"}, lot_sizes={"A": 100},
        )


def test_decision_cards_contain_required_fields(cfg):
    from wealth_quant.risk.limits import RiskEngine

    risk_engine = RiskEngine(cfg.risk_policy, secret=b"decision-test")
    as_of = date(2025, 1, 2)
    approved = pd.Series({"A": 0.15, "B": 0.10})
    target = TargetPortfolio(
        as_of=as_of, strategy_id="TEST", strategy_name="测试策略", version="1.0.0",
        weights=approved, rationale={"A": ["动量靠前"], "B": ["低波动"]},
        invalidation={"A": ["跌破 200 日线"], "B": ["波动率突升"]},
    )
    assessment = RiskAssessment(as_of=as_of, state=GREEN)
    approval = risk_engine._issue_approval(as_of, "TEST", approved, GREEN)
    decision = RiskDecision(
        assessment=assessment, approved_weights=approved, requested_weights=approved,
        rejected=False, reasons=[], approval=approval,
    )
    engine = DecisionEngine(risk_engine, require_risk_approval=True)
    batch = engine.build(
        as_of=as_of, target=target, risk=decision, current_weights=pd.Series(dtype=float),
        nav=1_000_000, prices={"A": 10.0, "B": 20.0},
        names={"A": "测试资产A", "B": "测试资产B"}, lot_sizes={"A": 100, "B": 100},
    )
    assert batch.approval_verified is True
    assert len(batch.cards) == 2
    card = batch.cards[0]
    for field in ("instrument_id", "signal", "current_weight", "target_weight",
                  "suggested_amount", "risk_level", "rationale", "invalidation", "horizon"):
        assert hasattr(card, field)
    assert card.suggested_shares % 100 == 0
    assert batch.manual_confirmation_required is True
    assert batch.auto_execution is False
    assert len(batch.disclaimers) >= 2
