"""因子标准化与打分引擎。

严格遵循建设手册：不把所有因子简单相加；先做异常值处理（MAD 缩尾）、
标准化（横截面 Z-Score）、方向统一与缺失处理，再加权形成评分，并保留因子贡献度
用于解释“为什么产生这个信号”。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np
import pandas as pd

# 方向：+1 数值越大越好；-1 数值越小越好
DIRECTIONS: dict[str, int] = {
    "ep": 1, "bp": 1, "sp": 1, "dividend_yield": 1, "fcf_yield": 1,
    "roe": 1, "roa": 1, "profit_growth": 1, "ocf_to_profit": 1,
    "debt_ratio": -1, "earnings_stability": 1,
    "mom_21": 1, "mom_63": 1, "mom_126": 1, "mom_252": 1, "rel_strength": 1,
    "vol_60": -1, "vol_120": -1, "downside_vol": -1, "max_drawdown": -1, "beta": -1,
}


def winsorize_mad(series: pd.Series, k: float = 3.0) -> pd.Series:
    """MAD 缩尾：把极端值拉回到中位数 ± k*1.4826*MAD。"""
    s = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan)
    if s.notna().sum() < 3:
        return s
    median = s.median()
    mad = (s - median).abs().median()
    if not np.isfinite(mad) or mad == 0:
        return s
    scale = 1.4826 * mad
    return s.clip(median - k * scale, median + k * scale)


def zscore(series: pd.Series) -> pd.Series:
    s = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan)
    std = s.std(ddof=0)
    if not np.isfinite(std) or std == 0:
        return pd.Series(0.0, index=s.index)
    return (s - s.mean()) / std


def industry_neutralize(z: pd.Series, industry: pd.Series | None) -> pd.Series:
    """行业中性：减去同行业均值，避免组合被单一行业主导。"""
    if industry is None:
        return z
    aligned = industry.reindex(z.index).fillna("未分类")
    group_mean = z.groupby(aligned).transform("mean")
    return z - group_mean


@dataclass
class FactorScore:
    score: pd.Series                 # 综合得分（缺失严重的行为 NaN）
    contributions: pd.DataFrame      # 各因子对综合得分的贡献
    coverage: pd.Series              # 每行有效因子占比
    zscores: pd.DataFrame

    def top_contributors(self, instrument_id: str, n: int = 3) -> list[tuple[str, float]]:
        if instrument_id not in self.contributions.index:
            return []
        row = self.contributions.loc[instrument_id].dropna()
        if row.empty:
            return []
        ordered = row.reindex(row.abs().sort_values(ascending=False).index)
        return [(str(k), float(v)) for k, v in ordered.head(n).items()]


def composite(
    factors: Mapping[str, pd.Series],
    weights: Mapping[str, float],
    directions: Mapping[str, int] | None = None,
    industry: pd.Series | None = None,
    min_coverage: float = 0.6,
    winsor_k: float = 3.0,
) -> FactorScore:
    """按权重合成综合评分。

    - 每个因子先缩尾、再横截面标准化、再按方向统一；
    - 缺失因子记为中性 0，但有效覆盖率低于 min_coverage 的行直接判为无效（NaN）；
    - 贡献度 = 标准化值 × 归一化权重。
    """
    if not factors:
        raise ValueError("至少需要一个因子")
    dirs = dict(DIRECTIONS)
    if directions:
        dirs.update(directions)

    z_cols: dict[str, pd.Series] = {}
    for name, raw in factors.items():
        series = pd.to_numeric(raw, errors="coerce").astype(float)
        series = winsorize_mad(series, k=winsor_k)
        z = zscore(series)
        z = industry_neutralize(z, industry)
        z = z * dirs.get(name, 1)
        z_cols[name] = z

    zframe = pd.DataFrame(z_cols)
    coverage = zframe.notna().mean(axis=1)
    total_weight = sum(abs(float(w)) for w in weights.values()) or 1.0
    norm_weights = {k: float(w) / total_weight for k, w in weights.items() if k in zframe.columns}
    if not norm_weights:
        raise ValueError("权重与因子名称不匹配")

    filled = zframe.fillna(0.0)
    contributions = pd.DataFrame(
        {name: filled[name] * weight for name, weight in norm_weights.items()},
        index=zframe.index,
    )
    score = contributions.sum(axis=1)
    score = score.where(coverage >= min_coverage)
    return FactorScore(score=score, contributions=contributions, coverage=coverage, zscores=zframe)
