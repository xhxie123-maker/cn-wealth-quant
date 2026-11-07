"""风险与绩效指标：CAGR、波动、Sharpe、Sortino、回撤、Calmar、VaR、ES、Beta、Alpha。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def daily_returns(nav: pd.Series) -> pd.Series:
    return nav.astype(float).pct_change(fill_method=None).dropna()


def cagr(nav: pd.Series, periods_per_year: int = TRADING_DAYS) -> float:
    if len(nav) < 2 or nav.iloc[0] <= 0:
        return float("nan")
    total = nav.iloc[-1] / nav.iloc[0]
    years = (len(nav) - 1) / periods_per_year
    if years <= 0:
        return float("nan")
    return float(total ** (1 / years) - 1)


def annualized_volatility(returns: pd.Series, periods_per_year: int = TRADING_DAYS) -> float:
    if returns.empty:
        return float("nan")
    return float(returns.std(ddof=0) * np.sqrt(periods_per_year))


def downside_deviation(returns: pd.Series, target: float = 0.0, periods_per_year: int = TRADING_DAYS) -> float:
    if returns.empty:
        return float("nan")
    downside = np.minimum(returns - target / periods_per_year, 0.0)
    return float(np.sqrt(np.mean(np.square(downside))) * np.sqrt(periods_per_year))


def sharpe(returns: pd.Series, risk_free_rate: float = 0.015, periods_per_year: int = TRADING_DAYS) -> float:
    vol = annualized_volatility(returns, periods_per_year)
    if vol <= 0 or not np.isfinite(vol):
        return float("nan")
    excess = returns.mean() * periods_per_year - risk_free_rate
    return float(excess / vol)


def sortino(returns: pd.Series, risk_free_rate: float = 0.015, periods_per_year: int = TRADING_DAYS) -> float:
    dd = downside_deviation(returns, target=risk_free_rate, periods_per_year=periods_per_year)
    if dd <= 0 or not np.isfinite(dd):
        return float("nan")
    excess = returns.mean() * periods_per_year - risk_free_rate
    return float(excess / dd)


@dataclass
class DrawdownInfo:
    max_drawdown: float
    peak_date: str | None
    trough_date: str | None
    recovery_date: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_drawdown": self.max_drawdown,
            "peak_date": self.peak_date,
            "trough_date": self.trough_date,
            "recovery_date": self.recovery_date,
        }


def max_drawdown(nav: pd.Series) -> DrawdownInfo:
    if nav.empty:
        return DrawdownInfo(float("nan"), None, None, None)
    running_max = nav.cummax()
    drawdown = nav / running_max - 1
    trough_idx = drawdown.idxmin()
    mdd = float(drawdown.min())
    peak_idx = nav.loc[:trough_idx].idxmax()
    recovery = nav.loc[trough_idx:]
    peak_value = nav.loc[peak_idx]
    recovered = recovery[recovery >= peak_value]
    recovery_idx = recovered.index[0] if len(recovered) else None

    def _fmt(idx):
        if idx is None:
            return None
        return str(getattr(idx, "date", lambda: idx)())

    return DrawdownInfo(mdd, _fmt(peak_idx), _fmt(trough_idx), _fmt(recovery_idx))


def calmar(nav: pd.Series) -> float:
    mdd = abs(max_drawdown(nav).max_drawdown)
    if mdd <= 0 or not np.isfinite(mdd):
        return float("nan")
    return float(cagr(nav) / mdd)


def var_es(returns: pd.Series, level: float = 0.95) -> tuple[float, float]:
    """历史模拟法 VaR / ES（返回负数，表示损失）。"""
    if returns.empty:
        return (float("nan"), float("nan"))
    alpha = 1 - level
    var = float(np.quantile(returns.dropna(), alpha))
    tail = returns[returns <= var]
    es = float(tail.mean()) if not tail.empty else var
    return (var, es)


def beta_alpha(returns: pd.Series, benchmark: pd.Series, periods_per_year: int = TRADING_DAYS) -> tuple[float, float]:
    aligned = pd.concat([returns, benchmark], axis=1, join="inner").dropna()
    if len(aligned) < 20:
        return (float("nan"), float("nan"))
    r = aligned.iloc[:, 0]
    b = aligned.iloc[:, 1]
    var_b = b.var(ddof=0)
    if var_b <= 0:
        return (float("nan"), float("nan"))
    beta = float(r.cov(b) / var_b)
    alpha_daily = float(r.mean() - beta * b.mean())
    alpha = (1 + alpha_daily) ** periods_per_year - 1
    return (beta, float(alpha))


def information_ratio(returns: pd.Series, benchmark: pd.Series) -> float:
    aligned = pd.concat([returns, benchmark], axis=1, join="inner").dropna()
    if aligned.empty:
        return float("nan")
    active = aligned.iloc[:, 0] - aligned.iloc[:, 1]
    te = active.std(ddof=0) * np.sqrt(TRADING_DAYS)
    if te <= 0:
        return float("nan")
    return float(active.mean() * TRADING_DAYS / te)


def concentration_hhi(weights: pd.Series) -> float:
    w = weights[weights > 0]
    if w.empty:
        return 0.0
    total = w.sum()
    if total <= 0:
        return 0.0
    return float(((w / total) ** 2).sum())


def portfolio_risk_contributions(weights: pd.Series, cov: pd.DataFrame) -> pd.Series:
    aligned = cov.reindex(index=weights.index, columns=weights.index).fillna(0.0)
    w = weights.reindex(aligned.index).fillna(0.0).to_numpy()
    mat = aligned.to_numpy()
    var = float(w @ mat @ w)
    if var <= 0:
        return pd.Series(0.0, index=weights.index)
    return pd.Series(w * (mat @ w) / var, index=weights.index)


def summarize(
    nav: pd.Series,
    benchmark: pd.Series | None = None,
    fills: pd.DataFrame | None = None,
    risk_free_rate: float = 0.015,
) -> dict[str, Any]:
    returns = daily_returns(nav)
    bench_returns = daily_returns(benchmark) if benchmark is not None and len(benchmark) > 1 else None
    dd = max_drawdown(nav)
    var95, es95 = var_es(returns, 0.95)
    var99, es99 = var_es(returns, 0.99)
    beta, alpha = beta_alpha(returns, bench_returns) if bench_returns is not None else (float("nan"), float("nan"))
    ir = information_ratio(returns, bench_returns) if bench_returns is not None else float("nan")
    turnover = float("nan")
    win_rate = float("nan")
    trades_count = 0
    if fills is not None and not fills.empty:
        trades_count = int(len(fills))
        if "realized_pnl" in fills.columns:
            closed = fills[fills["realized_pnl"].notna()]
            if len(closed):
                win_rate = float((closed["realized_pnl"] > 0).mean())
    return {
        "start": str(nav.index[0].date()) if len(nav) else None,
        "end": str(nav.index[-1].date()) if len(nav) else None,
        "days": int(len(nav)),
        "cagr": float(cagr(nav)),
        "annualized_volatility": annualized_volatility(returns),
        "sharpe": sharpe(returns, risk_free_rate),
        "sortino": sortino(returns, risk_free_rate),
        "max_drawdown": dd.max_drawdown,
        "max_drawdown_peak": dd.peak_date,
        "max_drawdown_trough": dd.trough_date,
        "max_drawdown_recovery": dd.recovery_date,
        "calmar": calmar(nav),
        "var_95": var95,
        "es_95": es95,
        "var_99": var99,
        "es_99": es99,
        "beta": beta,
        "alpha": alpha,
        "information_ratio": ir,
        "win_rate": win_rate,
        "trades": trades_count,
        "final_nav": float(nav.iloc[-1]) if len(nav) else float("nan"),
        "total_return": float(nav.iloc[-1] / nav.iloc[0] - 1) if len(nav) else float("nan"),
    }
