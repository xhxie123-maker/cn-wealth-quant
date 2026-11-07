"""数据源适配：离线示例数据、CSV 数据、AkShare（可选）。

设计原则：数据源可替换，上层只依赖 Dataset 契约；每条数据带来源与三个时间戳。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .calendar import TradingCalendar, weekdays_excluding_holidays
from .schema import (
    CORPORATE_ACTION_COLUMNS,
    Dataset,
    FUNDAMENTAL_COLUMNS,
    MACRO_COLUMNS,
    normalize_bars,
    normalize_fundamentals,
    normalize_instruments,
    Instrument,
)

REGIME_SEQUENCE = [
    ("bull", 0.00055, 0.0125),
    ("range", 0.00005, 0.0095),
    ("bear", -0.00065, 0.0165),
    ("high_vol", -0.00010, 0.0270),
]


def _quarter_ends(start: date, end: date) -> list[date]:
    out: list[date] = []
    for year in range(start.year, end.year + 1):
        for month, day in ((3, 31), (6, 30), (9, 30), (12, 31)):
            d = date(year, month, day)
            if start <= d <= end:
                out.append(d)
    return out


def generate_synthetic_dataset(
    instruments_spec: Iterable[dict[str, Any]],
    start: date = date(2021, 1, 4),
    end: date = date(2026, 6, 30),
    seed: int = 20261107,
) -> Dataset:
    """生成可复现的离线演示数据（非真实行情，仅用于验证工程链路）。"""
    rng = np.random.default_rng(seed)
    calendar = TradingCalendar(tuple(weekdays_excluding_holidays(start, end)))
    n_days = len(calendar.dates)

    specs: list[dict[str, Any]] = []
    for idx, raw in enumerate(instruments_spec):
        asset_type = str(raw.get("asset_type", "stock"))
        spec = dict(raw)
        spec.setdefault("instrument_id", raw.get("id"))
        spec["shares_outstanding"] = float(raw.get("shares_outstanding", 2.0e9))
        spec["_idx"] = idx
        if asset_type == "stock":
            spec.setdefault("beta", 0.85 + 0.12 * idx)
            spec.setdefault("idio_vol", 0.013)
            spec.setdefault("alpha", 0.00010)
            spec.setdefault("base_price", 20.0 + 8.0 * idx)
            spec.setdefault("base_revenue", 8.0e9 + 5.0e9 * idx)
            spec.setdefault("net_margin", 0.12 + 0.02 * idx)
            spec.setdefault("growth", 0.10 + 0.02 * idx)
            spec.setdefault("equity_ratio", 0.45 + 0.03 * idx)
        elif asset_type == "money_etf":
            spec.setdefault("beta", 0.01)
            spec.setdefault("idio_vol", 0.0004)
            spec.setdefault("alpha", 0.00005)
            spec.setdefault("base_price", 100.0)
        elif asset_type == "bond_etf":
            spec.setdefault("beta", -0.12)
            spec.setdefault("idio_vol", 0.0022)
            spec.setdefault("alpha", 0.00006)
            spec.setdefault("base_price", 118.0)
        elif asset_type == "gold_etf":
            spec.setdefault("beta", -0.05)
            spec.setdefault("idio_vol", 0.0085)
            spec.setdefault("alpha", 0.00012)
            spec.setdefault("base_price", 3.8)
        else:  # 宽基 ETF
            spec.setdefault("beta", 1.0)
            spec.setdefault("idio_vol", 0.0045)
            spec.setdefault("alpha", 0.00005)
            spec.setdefault("base_price", 4.0 + 2.0 * (idx % 3))
        specs.append(spec)

    # ----- 市场状态因子 -----
    mkt = np.zeros(n_days)
    for i in range(n_days):
        regime = REGIME_SEQUENCE[(i // 130) % len(REGIME_SEQUENCE)]
        drift, vol = regime[1], regime[2]
        mkt[i] = drift + vol * rng.standard_normal()

    rows: list[dict[str, Any]] = []
    split_events: list[dict[str, Any]] = []
    action_rows: list[dict[str, Any]] = []

    for spec in specs:
        iid = spec["instrument_id"]
        beta = float(spec["beta"])
        idio = float(spec["idio_vol"])
        alpha = float(spec["alpha"])
        price = float(spec["base_price"])
        rule_limit = 0.05 if spec.get("is_st") else (0.20 if spec.get("board") in {"chinext", "star"} else 0.10)
        if spec["asset_type"] != "stock":
            rule_limit = 0.10
        if spec["asset_type"] in {"bond_etf", "money_etf"}:
            rule_limit = 0.02

        adj = 1.0
        split_dates: dict[date, float] = {}
        if iid == "300750.SZ":
            split_dates[date(2023, 6, 15)] = 2.0

        for i, day in enumerate(calendar.dates):
            ret = alpha + beta * mkt[i] + idio * rng.standard_normal()
            # 避免生成长期封死在涨跌停上的价格
            ret = float(np.clip(ret, -rule_limit * 0.92, rule_limit * 0.92))

            prev_price = price
            if day in split_dates:
                ratio = split_dates[day]
                split_events.append({"instrument_id": iid, "ex_date": day, "ratio": ratio})
                action_rows.append({
                    "instrument_id": iid, "ex_date": pd.Timestamp(day), "action_type": "split",
                    "value": ratio, "available_at": pd.Timestamp(day) - pd.Timedelta(days=5),
                })
                prev_price = prev_price / ratio
                adj *= ratio

            price = max(0.5, prev_price * (1 + ret))
            gap = 0.18 * ret + 0.0025 * rng.standard_normal()
            open_px = prev_price * (1 + gap)
            limit_up = round(prev_price * (1 + rule_limit) + 1e-9, 2)
            limit_down = round(prev_price * (1 - rule_limit) + 1e-9, 2)
            open_px = float(np.clip(open_px, limit_down, limit_up))
            close_px = float(np.clip(price, limit_down, limit_up))
            high_px = max(open_px, close_px) * (1 + abs(rng.standard_normal()) * 0.004)
            low_px = min(open_px, close_px) * (1 - abs(rng.standard_normal()) * 0.004)
            high_px = float(min(high_px, limit_up))
            low_px = float(max(low_px, limit_down, 0.01))

            base_turnover = 0.0035 if spec["asset_type"] == "stock" else 0.008
            volume = float(spec["shares_outstanding"] * base_turnover * np.exp(0.30 * rng.standard_normal()))
            volume = max(1000.0, round(volume / 100) * 100)
            amount = volume * (open_px + close_px) / 2
            ts = pd.Timestamp(day)
            rows.append({
                "instrument_id": iid,
                "trade_date": ts,
                "open": round(open_px, 3),
                "high": round(high_px, 3),
                "low": round(low_px, 3),
                "close": round(close_px, 3),
                "volume": volume,
                "amount": round(amount, 2),
                "adj_factor": round(adj, 8),
                "is_suspended": False,
                "source": "synthetic",
                "event_time": ts + pd.Timedelta(hours=15),
                "published_at": ts + pd.Timedelta(hours=15, minutes=30),
                "available_at": ts + pd.Timedelta(hours=16),
            })

            # 现金分红：股票每年 6 月 20 日（若为交易日）每股派息约为价格的 1.2%
            if spec["asset_type"] == "stock" and day.month == 6 and day.day == 20:
                dps = round(close_px * 0.012, 3)
                action_rows.append({
                    "instrument_id": iid, "ex_date": ts, "action_type": "cash_dividend",
                    "value": dps, "available_at": ts - pd.Timedelta(days=10),
                })

    bars = normalize_bars(pd.DataFrame(rows))
    instruments = normalize_instruments(pd.DataFrame([{
        "instrument_id": s["instrument_id"],
        "name": s.get("name", s["instrument_id"]),
        "asset_type": s["asset_type"],
        "exchange": s.get("exchange", "SSE"),
        "board": s.get("board", "main"),
        "industry": s.get("industry", "未分类"),
        "is_st": s.get("is_st", False),
        "shares_outstanding": s["shares_outstanding"],
    } for s in specs]))

    # ----- 季度财务数据（point-in-time） -----
    fund_rows: list[dict[str, Any]] = []
    for spec in specs:
        if spec["asset_type"] != "stock":
            continue
        shares = spec["shares_outstanding"]
        base_rev = float(spec["base_revenue"])
        base_margin = float(spec["net_margin"])
        growth = float(spec["growth"])
        equity_ratio = float(spec["equity_ratio"])
        for q_idx, period_end in enumerate(_quarter_ends(start, date(end.year, 12, 31))):
            published = datetime.combine(period_end + timedelta(days=45), dtime(15, 30))
            available = published + timedelta(hours=3)
            if published.date() > end:
                continue
            year_frac = q_idx / 4
            rev = base_rev * ((1 + growth) ** year_frac) * (1 + 0.08 * rng.standard_normal())
            margin = base_margin + 0.02 * rng.standard_normal()
            net_profit = rev * margin
            equity = net_profit * (1 / max(equity_ratio, 0.05)) * 0.85
            assets = equity / equity_ratio
            ocf = net_profit * (0.9 + 0.25 * rng.standard_normal())
            debt_ratio = 1 - equity_ratio + 0.03 * rng.standard_normal()
            roe = net_profit / max(equity, 1)
            roa = net_profit / max(assets, 1)
            prev_year_rev = base_rev * ((1 + growth) ** max(year_frac - 1, 0))
            profit_growth = (rev - prev_year_rev) / max(prev_year_rev, 1)
            fund_rows.append({
                "instrument_id": spec["instrument_id"],
                "period_end": pd.Timestamp(period_end),
                "revenue": rev,
                "net_profit": net_profit,
                "total_equity": equity,
                "total_assets": assets,
                "operating_cash_flow": ocf,
                "debt_ratio": float(np.clip(debt_ratio, 0.05, 0.95)),
                "roe": roe,
                "roa": roa,
                "profit_growth": profit_growth,
                "earnings_stability": float(np.clip(0.55 + 0.30 * rng.random(), 0, 1)),
                "shares_outstanding": shares,
                "event_time": pd.Timestamp(period_end),
                "published_at": pd.Timestamp(published),
                "available_at": pd.Timestamp(available),
            })
    fundamentals = normalize_fundamentals(pd.DataFrame(fund_rows))

    actions = pd.DataFrame(action_rows)
    if not actions.empty:
        actions["ex_date"] = pd.to_datetime(actions["ex_date"])
        actions = actions[CORPORATE_ACTION_COLUMNS]

    # ----- 宏观月度数据 -----
    macro_rows: list[dict[str, Any]] = []
    month_ends = pd.date_range(start=pd.Timestamp(start), end=pd.Timestamp(end), freq="ME")
    for i, month_end in enumerate(month_ends):
        published = month_end + pd.Timedelta(days=10)
        spec_values = {
            "CPI_YOY": 1.2 + 0.8 * np.sin(i / 6) + 0.3 * rng.standard_normal(),
            "PMI": 50.2 + 1.4 * np.sin(i / 5) + 0.6 * rng.standard_normal(),
            "POLICY_RATE": 3.0 - 0.15 * (i // 8),
            "YIELD_10Y": 2.85 + 0.25 * np.sin(i / 9) + 0.05 * rng.standard_normal(),
        }
        for name, value in spec_values.items():
            macro_rows.append({
                "indicator": name,
                "period": month_end,
                "value": float(value),
                "published_at": published,
                "available_at": published + pd.Timedelta(hours=6),
            })
    macro = pd.DataFrame(macro_rows, columns=MACRO_COLUMNS)

    # ----- 基准指数（沪深300 代理） -----
    stock_specs = [s for s in specs if s["asset_type"] == "stock"]
    if stock_specs:
        stock_ids = [s["instrument_id"] for s in stock_specs]
        close_panel = bars[bars["instrument_id"].isin(stock_ids)].pivot(
            index="trade_date", columns="instrument_id", values="close"
        ).sort_index()
        weights = np.array([s["shares_outstanding"] for s in stock_specs], dtype=float)
        returns = close_panel.pct_change(fill_method=None).fillna(0.0)
        bench_ret = returns.to_numpy() @ (weights / weights.sum())
        bench = pd.Series(1000.0 * np.cumprod(1 + bench_ret), index=close_panel.index, name="000300.SH")
    else:
        bench = pd.Series(dtype=float, name="000300.SH")

    return Dataset(
        instruments=instruments.set_index("instrument_id", drop=False),
        bars=bars,
        calendar=calendar,
        fundamentals=fundamentals,
        corporate_actions=actions,
        macro=macro,
        benchmark=bench,
        source="synthetic",
    )


class CSVProvider:
    """从目录加载标准 CSV（bars.csv / instruments.csv / fundamentals.csv / ...）。"""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)

    def _read(self, name: str, required: bool = False) -> pd.DataFrame:
        path = self.directory / name
        if not path.exists():
            if required:
                raise FileNotFoundError(f"缺少数据文件: {path}")
            return pd.DataFrame()
        return pd.read_csv(path)

    def load(self) -> Dataset:
        instruments = normalize_instruments(self._read("instruments.csv", required=True))
        bars = normalize_bars(self._read("bars.csv", required=True))
        fundamentals = normalize_fundamentals(self._read("fundamentals.csv"))
        actions = self._read("corporate_actions.csv")
        if not actions.empty:
            actions["ex_date"] = pd.to_datetime(actions["ex_date"])
            for col in ("available_at",):
                if col in actions:
                    actions[col] = pd.to_datetime(actions[col])
        macro = self._read("macro.csv")
        if not macro.empty:
            for col in ("period", "published_at", "available_at"):
                macro[col] = pd.to_datetime(macro[col])
        calendar_df = self._read("trading_calendar.csv")
        if not calendar_df.empty:
            calendar = TradingCalendar.from_frame(calendar_df)
        else:
            calendar = TradingCalendar.from_series(bars["trade_date"].unique())
        bench_df = self._read("benchmark.csv")
        benchmark = None
        if not bench_df.empty:
            benchmark = pd.Series(
                bench_df["close"].to_numpy(),
                index=pd.to_datetime(bench_df["trade_date"]),
                name="benchmark",
            )
        return Dataset(
            instruments=instruments,
            bars=bars,
            calendar=calendar,
            fundamentals=fundamentals,
            corporate_actions=actions,
            macro=macro,
            benchmark=benchmark,
            source=str(self.directory),
        )


class AkshareProvider:
    """AkShare 行情适配（可选依赖）。仅覆盖日线行情；财务与宏观请接入持牌数据源。"""

    def __init__(self) -> None:
        try:
            import akshare as ak  # noqa: F401
        except ImportError as exc:  # pragma: no cover - 依赖缺失路径
            raise ImportError("使用 akshare 数据源需要先安装：pip install akshare") from exc
        import akshare as ak

        self._ak = ak

    def fetch_bars(self, symbols: list[str], start: date, end: date) -> pd.DataFrame:  # pragma: no cover
        frames = []
        for symbol in symbols:
            code, suffix = symbol.split(".") if "." in symbol else (symbol.split(".")[0], "SH")
            if suffix in {"SH", "SZ"} and code.startswith(("51", "56", "58", "15", "16")):
                df = self._ak.fund_etf_hist_em(
                    symbol=code, period="daily",
                    start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"), adjust="",
                )
            else:
                df = self._ak.stock_zh_a_hist(
                    symbol=code, period="daily",
                    start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"), adjust="",
                )
            if df is None or df.empty:
                continue
            df = df.rename(columns={
                "日期": "trade_date", "开盘": "open", "收盘": "close", "最高": "high",
                "最低": "low", "成交量": "volume", "成交额": "amount",
            })
            df["instrument_id"] = symbol
            df["adj_factor"] = 1.0
            df["source"] = "akshare"
            df["event_time"] = pd.to_datetime(df["trade_date"]) + pd.Timedelta(hours=15)
            df["published_at"] = pd.to_datetime(df["trade_date"]) + pd.Timedelta(hours=15, minutes=30)
            df["available_at"] = pd.to_datetime(df["trade_date"]) + pd.Timedelta(hours=16)
            frames.append(df[[
                "instrument_id", "trade_date", "open", "high", "low", "close", "volume",
                "amount", "adj_factor", "source", "event_time", "published_at", "available_at",
            ]])
        if not frames:
            raise RuntimeError("akshare 未返回任何行情数据，请检查代码、日期或网络")
        return normalize_bars(pd.concat(frames, ignore_index=True))
