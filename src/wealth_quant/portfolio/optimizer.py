"""组合优化器：等权、逆波动、风险平价、最小方差、带约束优化。

不依赖 scipy：风险平价用乘性迭代，最小方差用投影梯度，约束用迭代裁剪。
模型只负责“在约束下分配风险预算”，不预测收益；预期收益类优化默认关闭。
"""

from __future__ import annotations

from typing import Mapping

import numpy as np
import pandas as pd


def clean_cov(cov: pd.DataFrame, min_eig: float = 1e-10) -> pd.DataFrame:
    """对称化 + 特征值下限，避免数值问题导致权重爆炸。"""
    if cov.empty:
        return cov
    mat = cov.to_numpy(dtype=float)
    mat = np.nan_to_num(mat, nan=0.0, posinf=0.0, neginf=0.0)
    mat = (mat + mat.T) / 2
    eigvals, eigvecs = np.linalg.eigh(mat)
    eigvals = np.clip(eigvals, min_eig, None)
    fixed = eigvecs @ np.diag(eigvals) @ eigvecs.T
    return pd.DataFrame(fixed, index=cov.index, columns=cov.columns)


def shrink_cov(cov: pd.DataFrame, shrink: float = 0.20) -> pd.DataFrame:
    """收缩到对角矩阵（Ledoit-Wolf 的简化版），降低样本协方差噪声。"""
    cov = clean_cov(cov)
    diag = pd.DataFrame(np.diag(np.diag(cov.to_numpy())), index=cov.index, columns=cov.columns)
    return shrink * diag + (1 - shrink) * cov


def equal_weight(assets: list[str]) -> pd.Series:
    if not assets:
        return pd.Series(dtype=float)
    return pd.Series(1.0 / len(assets), index=assets)


def inverse_vol(cov: pd.DataFrame) -> pd.Series:
    vol = np.sqrt(np.clip(np.diag(cov.to_numpy()), 1e-12, None))
    inv = 1.0 / vol
    w = inv / inv.sum()
    return pd.Series(w, index=cov.index)


def risk_parity(cov: pd.DataFrame, max_iter: int = 500, tol: float = 1e-9) -> pd.Series:
    """风险平价：使各资产风险贡献相等（长仓，权重和为 1）。"""
    cov = clean_cov(cov)
    n = len(cov)
    if n == 0:
        return pd.Series(dtype=float)
    if n == 1:
        return pd.Series([1.0], index=cov.index)
    vol = np.sqrt(np.clip(np.diag(cov.to_numpy()), 1e-12, None))
    w = (1.0 / vol) / (1.0 / vol).sum()
    mat = cov.to_numpy()
    for _ in range(max_iter):
        mrc = mat @ w
        rc = w * mrc
        rc = np.clip(rc, 1e-15, None)
        target = rc.mean()
        w_new = w * np.sqrt(target / rc)
        w_new = np.clip(w_new, 1e-12, None)
        w_new = w_new / w_new.sum()
        if np.max(np.abs(w_new - w)) < tol:
            w = w_new
            break
        w = 0.5 * w + 0.5 * w_new
    return pd.Series(w, index=cov.index)


def min_variance(cov: pd.DataFrame, max_iter: int = 800, tol: float = 1e-10) -> pd.Series:
    """最小方差组合（长仓、权重和为 1），投影梯度法。"""
    cov = clean_cov(cov)
    n = len(cov)
    if n == 0:
        return pd.Series(dtype=float)
    mat = cov.to_numpy()
    eig_max = float(np.linalg.eigvalsh(mat).max())
    step = 1.0 / max(2.0 * eig_max, 1e-9)
    w = np.full(n, 1.0 / n)
    for _ in range(max_iter):
        grad = mat @ w
        w_new = _project_simplex(w - step * grad)
        if np.max(np.abs(w_new - w)) < tol:
            w = w_new
            break
        w = w_new
    return pd.Series(np.clip(w, 0, None), index=cov.index)


def _project_simplex(v: np.ndarray) -> np.ndarray:
    """欧氏投影到单纯形 {w >= 0, sum(w) = 1}。"""
    n = len(v)
    u = np.sort(v)[::-1]
    css = np.cumsum(u) - 1.0
    rho = np.nonzero(u - css / np.arange(1, n + 1) > 0)[0][-1]
    theta = css[rho] / (rho + 1)
    return np.clip(v - theta, 0, None)


def project_weights(
    weights: Mapping[str, float] | pd.Series,
    max_weight: float = 1.0,
    min_weight: float = 0.0,
    sector_map: Mapping[str, str] | None = None,
    max_sector: float | None = None,
    max_total: float = 1.0,
) -> pd.Series:
    """把任意权重投影到可执行约束域，并把被上限截掉的额度回补给仍有空间的资产。

    为什么要回补：风险平价遇到货币 ETF 这类近零波动资产时，单资产上限会截掉绝大部分
    权重；如果不回补，组合会退化成“90% 现金”，与策略意图不符。回补只在 cap/sector
    仍有余量时进行，因此不会突破约束。若所有资产都触顶，剩余部分自然留作现金。
    """
    if isinstance(weights, pd.Series):
        series = weights.astype(float).copy()
    else:
        series = pd.Series(dict(weights), dtype=float)
    if series.empty:
        return series
    series = series.replace([np.inf, -np.inf], np.nan).fillna(0.0).clip(lower=0.0)
    if series.sum() <= 0:
        return series * 0.0

    target_total = min(float(series.sum()), float(max_total))
    w = series / series.sum() * target_total
    sectors = None
    if sector_map is not None:
        sectors = pd.Series({k: sector_map.get(k, "未分类") for k in w.index})

    for _ in range(200):
        w = w.clip(upper=max_weight)
        if sectors is not None and max_sector is not None:
            totals = w.groupby(sectors).sum()
            for sector, total in totals.items():
                if total > max_sector + 1e-12:
                    members = sectors[sectors == sector].index
                    w.loc[members] = w.loc[members] * (max_sector / total)
        deficit = target_total - float(w.sum())
        if deficit <= 1e-12:
            break

        eligible = w < max_weight - 1e-9
        if sectors is not None and max_sector is not None:
            totals = w.groupby(sectors).sum()
            for iid in w.index[eligible]:
                if totals.get(sectors[iid], 0.0) >= max_sector - 1e-9:
                    eligible[iid] = False
        if not eligible.any():
            break

        base = series.reindex(w.index)[eligible]
        if base.sum() <= 0:
            base = pd.Series(1.0, index=base.index)
        w.loc[eligible] += deficit * base / base.sum()

    if min_weight > 0:
        w = w.where(w >= min_weight, 0.0)
    if w.sum() > max_total + 1e-12:
        w = w / w.sum() * max_total
    return w


def risk_contributions(weights: pd.Series, cov: pd.DataFrame) -> pd.Series:
    """各资产对组合方差的贡献（绝对值占比）。"""
    aligned = cov.reindex(index=weights.index, columns=weights.index).fillna(0.0)
    w = weights.reindex(aligned.index).fillna(0.0).to_numpy()
    mat = aligned.to_numpy()
    port_var = float(w @ mat @ w)
    if port_var <= 0:
        return pd.Series(0.0, index=weights.index)
    mrc = mat @ w
    rc = w * mrc / port_var
    return pd.Series(rc, index=weights.index)
