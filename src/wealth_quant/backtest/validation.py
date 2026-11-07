"""回测验证：样本内/样本外切分、滚动窗口、CSCV 组合对称交叉验证（PBO）。

参考：
- Bailey, Borwein, López de Prado, Zhu, "The Probability of Backtest Overfitting",
  Journal of Computational Finance, 2017. DOI: 10.21314/JCF.2016.322
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Any, Callable

import numpy as np
import pandas as pd


@dataclass
class PBOReport:
    pbo: float
    n_trials: int
    n_splits: int
    median_oos_rank: float
    logits: list[float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "pbo": self.pbo, "n_trials": self.n_trials, "n_splits": self.n_splits,
            "median_oos_rank": self.median_oos_rank,
            "interpretation": (
                "PBO 越低越稳健；>0.5 表示样本内最优策略在样本外大概率不再最优"
                if np.isfinite(self.pbo) else "样本不足，无法评估"
            ),
        }


def split_is_oos(nav: pd.Series, is_ratio: float = 0.7) -> tuple[pd.Series, pd.Series]:
    if not 0 < is_ratio < 1:
        raise ValueError("is_ratio 必须在 (0,1)")
    n = int(len(nav) * is_ratio)
    return nav.iloc[:n], nav.iloc[n:]


def walk_forward_windows(
    calendar_dates: list,
    train_days: int = 504,
    test_days: int = 126,
    step_days: int = 126,
) -> list[tuple[int, int, int, int]]:
    """返回 (train_start, train_end, test_start, test_end) 的索引区间。"""
    windows = []
    start = 0
    n = len(calendar_dates)
    while start + train_days + test_days <= n:
        windows.append((start, start + train_days, start + train_days, start + train_days + test_days))
        start += step_days
    return windows


def _column_sharpe(frame: pd.DataFrame) -> pd.Series:
    mean = frame.mean()
    std = frame.std(ddof=0)
    return (mean / std.replace(0, np.nan)) * np.sqrt(252)


def cscv_pbo(returns: pd.DataFrame, partitions: int = 10) -> PBOReport:
    """组合对称交叉验证估计回测过拟合概率。

    returns: index=时间, columns=不同参数/策略试验的日收益。
    将样本切成 S 个连续块，枚举 S/2 块作为 IS，其余作为 OOS；
    每次取 IS 最优试验，观察其在 OOS 的排名，计算 logit。
    """
    if returns is None or returns.empty:
        return PBOReport(float("nan"), 0, 0, float("nan"), [])
    frame = returns.dropna(how="all").fillna(0.0)
    if frame.columns.duplicated().any():
        # 同名 trial 会让 idxmax/get 返回 Series，直接破坏 PBO 计算
        frame = frame.loc[:, ~frame.columns.duplicated()]
    n_trials = frame.shape[1]
    if n_trials < 2:
        return PBOReport(float("nan"), n_trials, 0, float("nan"), [])
    if partitions % 2 != 0:
        partitions += 1
    block = len(frame) // partitions
    if block < 10:
        return PBOReport(float("nan"), n_trials, 0, float("nan"), [])
    trimmed = frame.iloc[: block * partitions]
    blocks = [trimmed.iloc[i * block:(i + 1) * block] for i in range(partitions)]
    logits: list[float] = []
    ranks: list[float] = []
    half = partitions // 2
    for combo in combinations(range(partitions), half):
        is_idx = list(combo)
        oos_idx = [i for i in range(partitions) if i not in combo]
        is_frame = pd.concat([blocks[i] for i in is_idx], axis=0)
        oos_frame = pd.concat([blocks[i] for i in oos_idx], axis=0)
        perf_is = _column_sharpe(is_frame)
        perf_oos = _column_sharpe(oos_frame)
        if perf_is.isna().all() or perf_oos.isna().all():
            continue
        best = perf_is.idxmax()
        best_oos = perf_oos.get(best, np.nan)
        if isinstance(best_oos, pd.Series):
            best_oos = float(best_oos.iloc[0])
        if not np.isfinite(best_oos):
            continue
        valid = perf_oos.dropna()
        rank = float((valid < best_oos).sum() + 1)
        omega = rank / (len(valid) + 1)
        omega = min(max(omega, 1e-6), 1 - 1e-6)
        logits.append(float(np.log(omega / (1 - omega))))
        ranks.append(rank / len(valid))
    if not logits:
        return PBOReport(float("nan"), n_trials, 0, float("nan"), [])
    pbo = float(np.mean([1.0 if x <= 0 else 0.0 for x in logits]))
    return PBOReport(pbo, n_trials, len(logits), float(np.median(ranks)), logits)


def compare_metrics(is_metrics: dict[str, Any], oos_metrics: dict[str, Any]) -> dict[str, Any]:
    """比较样本内外表现，识别“只在一段历史里有效”的策略。"""
    keys = ["cagr", "sharpe", "max_drawdown", "annualized_volatility"]
    out = {}
    for key in keys:
        a, b = is_metrics.get(key), oos_metrics.get(key)
        out[key] = {"is": a, "oos": b}
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and a not in (0, None):
            out[key]["change"] = (b - a) / abs(a)
    return out
