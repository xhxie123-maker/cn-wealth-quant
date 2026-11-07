"""因子库：价值、质量、动量、风险、流动性。全部按 point-in-time 计算。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Iterable

import numpy as np
import pandas as pd

from ..data.schema import Dataset, EQUITY_LIKE
from .engine import composite


@dataclass
class FactorSnapshot:
    as_of: date
    groups: dict[str, dict[str, pd.Series]] = field(default_factory=dict)
    meta: pd.DataFrame = field(default_factory=pd.DataFrame)

    def add(self, group: str, name: str, values: pd.Series) -> None:
        self.groups.setdefault(group, {})[name] = values

    def frame(self) -> pd.DataFrame:
        cols = {}
        for group, values in self.groups.items():
            for name, series in values.items():
                cols[f"{group}.{name}"] = series
        return pd.DataFrame(cols)

    @property
    def instrument_ids(self) -> list[str]:
        if self.meta is not None and not self.meta.empty:
            return self.meta.index.tolist()
        return []


def _last_price(dataset: Dataset) -> pd.Series:
    panel = dataset.price_panel("close", adjusted=False)
    if panel.empty:
        return pd.Series(dtype=float)
    return panel.ffill().iloc[-1]


def compute_momentum(dataset: Dataset, universe: Iterable[str], as_of: date) -> dict[str, pd.Series]:
    panel = dataset.price_panel("close", adjusted=True)
    if panel.empty:
        return {}
    panel = panel.ffill()
    ids = [i for i in universe if i in panel.columns]
    panel = panel[ids]
    out: dict[str, pd.Series] = {}
    for window in (21, 63, 126, 252):
        if len(panel) > window:
            out[f"mom_{window}"] = panel.iloc[-1] / panel.iloc[-1 - window] - 1
    if len(panel) > 252:
        out["rel_strength"] = (panel.iloc[-1] / panel.iloc[-21 - 1] - 1) - (panel.iloc[-252] / panel.iloc[-252 - 1] - 1)
    # 12-1 动量（策略层常用）
    if len(panel) > 252 + 21:
        out["mom_12_1"] = panel.iloc[-1 - 21] / panel.iloc[-1 - 21 - 252] - 1
    return out


def compute_risk(
    dataset: Dataset,
    universe: Iterable[str],
    benchmark: pd.Series | None = None,
) -> tuple[dict[str, pd.Series], pd.DataFrame]:
    """返回风险因子与年化协方差矩阵（用于组合优化）。"""
    panel = dataset.price_panel("close", adjusted=True)
    if panel.empty:
        return {}, pd.DataFrame()
    panel = panel.ffill()
    returns = panel.pct_change(fill_method=None)
    ids = [i for i in universe if i in returns.columns]
    returns = returns[ids]
    out: dict[str, pd.Series] = {}

    for window in (60, 120):
        if len(returns) >= window:
            out[f"vol_{window}"] = returns.tail(window).std(ddof=0) * np.sqrt(252)
    if len(returns) >= 120:
        downside = returns.tail(120).where(returns.tail(120) < 0)
        out["downside_vol"] = downside.std(ddof=0) * np.sqrt(252)
    if len(returns) >= 252:
        nav = (1 + returns.tail(252)).cumprod()
        drawdown = nav / nav.cummax() - 1
        out["max_drawdown"] = drawdown.min()
    if benchmark is not None and len(returns) >= 120:
        bench = benchmark.pct_change(fill_method=None).reindex(returns.index).tail(120)
        bench_var = bench.var(ddof=0)
        betas = {}
        for iid in ids:
            series = returns[iid].tail(120)
            cov = series.cov(bench)
            betas[iid] = cov / bench_var if bench_var and np.isfinite(bench_var) else np.nan
        out["beta"] = pd.Series(betas)

    amount = dataset.adv_panel(window=20)
    if not amount.empty:
        out["liquidity_amount"] = amount.iloc[-1].reindex(ids)

    cov = pd.DataFrame()
    if len(returns) >= 120:
        cov = returns.tail(252).cov() * 252
    return out, cov


def compute_value(
    dataset: Dataset,
    universe: Iterable[str],
    as_of: date,
) -> dict[str, pd.Series]:
    """价值因子：用决策时点的价格 + 最新可用财务（PIT）计算估值。"""
    fund = dataset.fundamentals_asof(as_of)
    if fund.empty:
        return {}
    price = _last_price(dataset)
    ids = [i for i in universe if i in fund.index and i in price.index]
    fund = fund.loc[ids]
    mcap = price.reindex(ids) * fund["shares_outstanding"]
    out: dict[str, pd.Series] = {}
    with np.errstate(divide="ignore", invalid="ignore"):
        # 单季数据年化（近似 TTM），生产应直接使用 TTM 口径
        out["ep"] = (fund["net_profit"] * 4) / mcap
        out["bp"] = fund["total_equity"] / mcap
        out["sp"] = (fund["revenue"] * 4) / mcap
        out["fcf_yield"] = (fund["operating_cash_flow"] * 4) / mcap

    actions = dataset.corporate_actions
    if actions is not None and not actions.empty:
        div = actions[actions["action_type"] == "cash_dividend"].copy()
        if not div.empty:
            div["ex_date"] = pd.to_datetime(div["ex_date"])
            cutoff = pd.Timestamp(as_of)
            last_year = div[(div["ex_date"] > cutoff - pd.Timedelta(days=370)) & (div["ex_date"] <= cutoff)]
            dps = last_year.groupby("instrument_id")["value"].sum()
            out["dividend_yield"] = (dps / price).reindex(ids)
    return out


def compute_quality(dataset: Dataset, universe: Iterable[str], as_of: date) -> dict[str, pd.Series]:
    fund = dataset.fundamentals_asof(as_of)
    if fund.empty:
        return {}
    ids = [i for i in universe if i in fund.index]
    fund = fund.loc[ids]
    out: dict[str, pd.Series] = {}
    out["roe"] = fund["roe"]
    out["roa"] = fund["roa"]
    out["profit_growth"] = fund["profit_growth"]
    with np.errstate(divide="ignore", invalid="ignore"):
        out["ocf_to_profit"] = fund["operating_cash_flow"] / fund["net_profit"]
    out["debt_ratio"] = fund["debt_ratio"]
    out["earnings_stability"] = fund["earnings_stability"]
    return out


def build_factor_snapshot(
    dataset: Dataset,
    universe: Iterable[str],
    as_of: date,
    benchmark: pd.Series | None = None,
    include_fundamentals: bool = True,
) -> tuple[FactorSnapshot, pd.DataFrame]:
    """构建时点因子快照，并返回年化协方差矩阵。"""
    ids = list(universe)
    snap = FactorSnapshot(as_of=as_of)
    for name, series in compute_momentum(dataset, ids, as_of).items():
        snap.add("momentum", name, series)
    risk_factors, cov = compute_risk(dataset, ids, benchmark)
    for name, series in risk_factors.items():
        snap.add("risk", name, series)
    if include_fundamentals:
        for name, series in compute_value(dataset, ids, as_of).items():
            snap.add("value", name, series)
        for name, series in compute_quality(dataset, ids, as_of).items():
            snap.add("quality", name, series)

    meta_rows = []
    instruments = dataset.instruments
    for iid in ids:
        if iid not in instruments.index:
            continue
        row = instruments.loc[iid]
        meta_rows.append({
            "instrument_id": iid,
            "name": row["name"],
            "asset_type": row["asset_type"],
            "industry": row["industry"],
            "is_st": bool(row["is_st"]),
            "close": _last_price(dataset).get(iid, np.nan),
            "avg_amount_20d": (dataset.adv_panel(20).iloc[-1].get(iid, np.nan)
                               if not dataset.adv_panel(20).empty else np.nan),
        })
    snap.meta = pd.DataFrame(meta_rows).set_index("instrument_id") if meta_rows else pd.DataFrame()
    return snap, cov


def composite_group(
    snapshot: FactorSnapshot,
    group: str,
    weights: dict[str, float],
    min_coverage: float = 0.6,
    industry: pd.Series | None = None,
):
    """对某一因子组做加权合成。"""
    values = snapshot.groups.get(group, {})
    if not values:
        raise ValueError(f"因子组为空: {group}")
    return composite(values, weights, industry=industry, min_coverage=min_coverage)


def investable_mask(snapshot: FactorSnapshot, min_amount: float = 0.0) -> pd.Series:
    """可投资性过滤：剔除 ST、流动性不足、价格缺失。"""
    meta = snapshot.meta
    if meta.empty:
        return pd.Series(dtype=bool)
    mask = pd.Series(True, index=meta.index)
    mask &= ~meta["is_st"].astype(bool)
    mask &= meta["close"].notna() & (meta["close"] > 0)
    if min_amount > 0 and "avg_amount_20d" in meta:
        mask &= meta["avg_amount_20d"].fillna(0) >= min_amount
    return mask
