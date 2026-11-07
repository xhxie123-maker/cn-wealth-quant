"""组合约束投影、目标契约、样本外窗口、PBO、审计哈希链。"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from wealth_quant.audit.log import AuditLog
from wealth_quant.backtest.validation import cscv_pbo, split_is_oos, walk_forward_windows
from wealth_quant.portfolio.optimizer import project_weights
from wealth_quant.strategies.base import TargetPortfolio


def test_projection_respects_single_asset_cap_and_redistributes():
    weights = pd.Series({"A": 0.90, "B": 0.05, "C": 0.05})
    projected = project_weights(weights, max_weight=0.20, max_total=1.0)
    assert float(projected.max()) <= 0.200001
    # 三个资产全部触顶时总仓位只能是 60%，余额自然留作现金
    assert abs(float(projected.sum()) - 0.60) < 1e-6


def test_projection_respects_sector_cap():
    weights = pd.Series({"A": 0.4, "B": 0.4, "C": 0.2})
    sector = {"A": "银行", "B": "银行", "C": "医药"}
    projected = project_weights(weights, max_weight=0.20, sector_map=sector, max_sector=0.35, max_total=1.0)
    assert float(projected[["A", "B"]].sum()) <= 0.350001
    assert float(projected.max()) <= 0.200001


def test_target_portfolio_rejects_overallocated_weights():
    with pytest.raises(ValueError):
        TargetPortfolio(
            as_of=date(2025, 1, 2),
            strategy_id="TEST",
            strategy_name="测试",
            version="1.0.0",
            weights=pd.Series({"A": 0.7, "B": 0.4}),
        )


def test_walk_forward_windows_are_strictly_ordered():
    dates = list(pd.bdate_range("2020-01-01", periods=1200))
    windows = walk_forward_windows(dates, train_days=504, test_days=126, step_days=126)
    assert len(windows) == 5
    for train_start, train_end, test_start, test_end in windows:
        assert train_start < train_end <= test_start < test_end
        assert train_end - train_start == 504
        assert test_end - test_start == 126


def test_cscv_pbo_returns_bounded_probability():
    rng = np.random.default_rng(42)
    returns = pd.DataFrame(rng.normal(0.0005, 0.01, size=(400, 5)),
                           columns=[f"trial_{i}" for i in range(5)])
    report = cscv_pbo(returns, partitions=10)
    assert report.n_trials == 5
    assert report.n_splits == 252  # C(10, 5) 个 IS/OOS 组合
    assert 0.0 <= report.pbo <= 1.0
    assert np.isfinite(report.median_oos_rank)


def test_cscv_pbo_tolerates_duplicate_trial_names():
    rng = np.random.default_rng(7)
    frame = pd.DataFrame(rng.normal(0.0004, 0.01, size=(300, 3)), columns=["nav", "nav", "nav"])
    report = cscv_pbo(frame, partitions=10)
    assert report.n_trials == 1  # 重复列名只保留第一列，避免 Series 真值异常
    assert np.isnan(report.pbo)


def test_split_is_oos_keeps_order():
    nav = pd.Series(np.linspace(1.0, 2.0, 100))
    is_part, oos_part = split_is_oos(nav, 0.7)
    assert len(is_part) == 70
    assert len(oos_part) == 30
    assert is_part.iloc[-1] <= oos_part.iloc[0]


def test_audit_chain_detects_tampering(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.append("run", {"strategy": "TEST", "nav": 1_000_000})
    log.append("decision", {"cards": 3})
    assert log.verify() is True

    lines = path.read_text(encoding="utf-8").splitlines()
    tampered = lines[0].replace("TEST", "HACK")
    path.write_text("\n".join([tampered, lines[1]]) + "\n", encoding="utf-8")
    assert log.verify() is False
