"""风控：结构投影、RED 降险、SYSTEM_HALT、HMAC 审批与篡改检测。"""

from __future__ import annotations

from datetime import date

import pandas as pd

from wealth_quant.data.quality import SEVERITY_ERROR, QualityIssue, QualityReport
from wealth_quant.risk.limits import HALT, RED, RiskEngine


def _engine(cfg) -> RiskEngine:
    return RiskEngine(cfg.risk_policy, secret=b"unit-test-secret")


def test_single_asset_cap_is_projected_before_assessment(cfg):
    engine = _engine(cfg)
    as_of = date(2025, 1, 2)
    target = pd.Series({"600519.SH": 0.90, "510300.SH": 0.05, "511880.SH": 0.05})
    decision = engine.review_target(
        as_of=as_of,
        target_weights=target,
        current_weights=pd.Series(dtype=float),
        sector_map={"600519.SH": "食品饮料", "510300.SH": "宽基", "511880.SH": "货币"},
        nav_history=pd.Series([1_000_000.0, 1_010_000.0]),
        nav_value=1_010_000.0,
        strategy_id="TEST",
    )
    assert float(decision.approved_weights.max()) <= 0.200001
    assert decision.approval is not None
    assert engine.verify_approval(
        decision.approval, as_of, "TEST", decision.approved_weights
    )


def test_red_state_uses_current_drawdown_and_caps_exposure(cfg):
    engine = _engine(cfg)
    as_of = date(2025, 1, 2)
    target = pd.Series({f"T{i}": 0.20 for i in range(5)})
    nav = pd.Series([1.0, 1.10, 1.02, 0.88, 0.80])  # 当前回撤 27%
    decision = engine.review_target(
        as_of=as_of,
        target_weights=target,
        current_weights=target,
        nav_history=nav * 1_000_000,
        nav_value=800_000,
        strategy_id="TEST_RED",
    )
    assert decision.state == RED
    assert float(decision.approved_weights.sum()) <= 0.401
    assert any("回撤" in reason or "RED" in reason for reason in decision.reasons)


def test_system_halt_blocks_new_positions(cfg):
    engine = _engine(cfg)
    quality = QualityReport(
        as_of=None,
        issues=[QualityIssue("stale_data", SEVERITY_ERROR, "行情超过 3 个交易日未更新", count=2)],
        checked_rows=0,
        instruments=1,
    )
    decision = engine.review_target(
        as_of=date(2025, 1, 2),
        target_weights=pd.Series({"A": 0.5, "B": 0.5}),
        current_weights=pd.Series(dtype=float),
        quality=quality,
        nav_history=pd.Series([1_000_000.0]),
        strategy_id="TEST_HALT",
    )
    assert decision.state == HALT
    assert decision.rejected
    assert decision.approval is None


def test_approval_detects_tampered_weights(cfg):
    engine = _engine(cfg)
    as_of = date(2025, 1, 2)
    target = pd.Series({"A": 0.5, "B": 0.3})
    decision = engine.review_target(
        as_of=as_of,
        target_weights=target,
        current_weights=pd.Series(dtype=float),
        nav_history=pd.Series([1_000_000.0]),
        strategy_id="TEST_TAMPER",
    )
    assert decision.approval is not None
    tampered = decision.approved_weights.copy()
    tampered.iloc[0] = tampered.iloc[0] + 0.01
    assert not engine.verify_approval(decision.approval, as_of, "TEST_TAMPER", tampered)
    assert not engine.verify_approval(decision.approval, date(2025, 1, 3), "TEST_TAMPER",
                                     decision.approved_weights)
    assert not engine.verify_approval(decision.approval, as_of, "OTHER", decision.approved_weights)
