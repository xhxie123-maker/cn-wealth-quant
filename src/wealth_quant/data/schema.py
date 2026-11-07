"""数据契约：字段规范、资产主数据、数据集装配与校验。

三个时间字段必须严格区分（防止未来数据泄漏）：
- event_time   市场事件实际发生的时间（如财报所属期末、除权日）
- published_at 数据对外公布的时间
- available_at 本系统实际可以拿到该数据的时间（>= published_at）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable

import pandas as pd

ASSET_TYPES = {
    "stock",
    "etf",
    "bond_etf",
    "money_etf",
    "gold_etf",
    "crossborder_etf",
    "bond",
}

EQUITY_LIKE = {"stock", "etf", "gold_etf", "crossborder_etf"}

BAR_COLUMNS = [
    "instrument_id",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "adj_factor",
    "is_suspended",
    "source",
    "event_time",
    "published_at",
    "available_at",
]

FUNDAMENTAL_COLUMNS = [
    "instrument_id",
    "period_end",
    "revenue",
    "net_profit",
    "total_equity",
    "total_assets",
    "operating_cash_flow",
    "debt_ratio",
    "roe",
    "roa",
    "profit_growth",
    "earnings_stability",
    "shares_outstanding",
    "event_time",
    "published_at",
    "available_at",
]

CORPORATE_ACTION_COLUMNS = ["instrument_id", "ex_date", "action_type", "value", "available_at"]

INSTRUMENT_COLUMNS = [
    "instrument_id",
    "name",
    "asset_type",
    "exchange",
    "board",
    "industry",
    "is_st",
    "shares_outstanding",
    "list_date",
    "delist_date",
]

MACRO_COLUMNS = ["indicator", "period", "value", "published_at", "available_at"]


@dataclass(frozen=True)
class Instrument:
    instrument_id: str
    name: str
    asset_type: str
    exchange: str = "SSE"
    board: str = "main"
    industry: str = "未分类"
    is_st: bool = False
    shares_outstanding: float = 0.0
    list_date: date | None = None
    delist_date: date | None = None

    def __post_init__(self) -> None:
        if self.asset_type not in ASSET_TYPES:
            raise ValueError(f"未知资产类型: {self.asset_type}")

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Instrument":
        def _date(value: Any) -> date | None:
            if value is None or (isinstance(value, float) and pd.isna(value)) or value == "":
                return None
            return pd.Timestamp(value).date()

        return cls(
            instrument_id=str(row["instrument_id"]),
            name=str(row.get("name", row["instrument_id"])),
            asset_type=str(row.get("asset_type", "stock")),
            exchange=str(row.get("exchange", "SSE")),
            board=str(row.get("board", "main")),
            industry=str(row.get("industry", "未分类")),
            is_st=bool(row.get("is_st", False)),
            shares_outstanding=float(row.get("shares_outstanding", 0) or 0),
            list_date=_date(row.get("list_date")),
            delist_date=_date(row.get("delist_date")),
        )


def normalize_instruments(frame: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in INSTRUMENT_COLUMNS if c not in frame.columns]
    if missing:
        for col, default in {
            "name": frame.get("instrument_id", ""),
            "asset_type": "stock",
            "exchange": "SSE",
            "board": "main",
            "industry": "未分类",
            "is_st": False,
            "shares_outstanding": 0.0,
            "list_date": pd.NaT,
            "delist_date": pd.NaT,
        }.items():
            if col in missing:
                frame[col] = default
    out = frame.copy()
    out["instrument_id"] = out["instrument_id"].astype(str)
    # CSV 读入的 "False"/"True" 字符串不能直接 astype(bool)
    out["is_st"] = out["is_st"].map(
        lambda v: v if isinstance(v, bool) else str(v).strip().lower() in {"true", "1", "yes", "y"}
    ).fillna(False).astype(bool)
    for col in ("list_date", "delist_date"):
        out[col] = pd.to_datetime(out[col], errors="coerce")
    if "shares_outstanding" in out:
        out["shares_outstanding"] = pd.to_numeric(out["shares_outstanding"], errors="coerce").fillna(0.0)
    return out.set_index("instrument_id", drop=False)


def normalize_bars(frame: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in ("instrument_id", "trade_date", "open", "high", "low", "close", "volume") if c not in frame]
    if missing:
        raise ValueError(f"行情数据缺少必需字段: {missing}")
    out = frame.copy()
    out["instrument_id"] = out["instrument_id"].astype(str)
    out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.normalize()
    for col in ("open", "high", "low", "close", "volume", "amount", "adj_factor"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    if "adj_factor" not in out:
        out["adj_factor"] = 1.0
    if "amount" not in out:
        out["amount"] = out["close"] * out["volume"]
    if "is_suspended" not in out:
        out["is_suspended"] = out["volume"].fillna(0) <= 0
    for col in ("event_time", "published_at", "available_at"):
        if col in out.columns:
            out[col] = pd.to_datetime(out[col], errors="coerce")
    out["adj_factor"] = out.groupby("instrument_id")["adj_factor"].ffill().fillna(1.0)
    out = out.sort_values(["instrument_id", "trade_date"]).drop_duplicates(["instrument_id", "trade_date"], keep="last")
    return out.reset_index(drop=True)


def normalize_fundamentals(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=FUNDAMENTAL_COLUMNS)
    out = frame.copy()
    out["instrument_id"] = out["instrument_id"].astype(str)
    for col in ("period_end", "event_time", "published_at", "available_at"):
        out[col] = pd.to_datetime(out[col])
    numeric = [
        "revenue", "net_profit", "total_equity", "total_assets", "operating_cash_flow",
        "debt_ratio", "roe", "roa", "profit_growth", "earnings_stability", "shares_outstanding",
    ]
    for col in numeric:
        if col not in out:
            out[col] = float("nan")
        out[col] = pd.to_numeric(out[col], errors="coerce")
    # 强制 available_at 不早于 published_at
    out["available_at"] = out[["available_at", "published_at"]].max(axis=1)
    return out.sort_values(["instrument_id", "available_at"]).reset_index(drop=True)


@dataclass
class Dataset:
    """内存数据集。所有模块只接收 Dataset，不直接读文件。"""

    instruments: pd.DataFrame
    bars: pd.DataFrame
    calendar: Any  # TradingCalendar
    fundamentals: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=FUNDAMENTAL_COLUMNS))
    corporate_actions: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=CORPORATE_ACTION_COLUMNS))
    macro: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=MACRO_COLUMNS))
    benchmark: pd.Series | None = None
    source: str = "unknown"

    # ---------- 基础查询 ----------
    @property
    def instrument_ids(self) -> list[str]:
        return self.instruments.index.tolist()

    def instrument(self, instrument_id: str) -> Instrument:
        row = self.instruments.loc[instrument_id]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        return Instrument.from_row(row.to_dict())

    def universe(self, include_types: Iterable[str] | None = None, exclude_st: bool = True,
                 max_instruments: int | None = None) -> list[str]:
        inst = self.instruments
        ids = inst.index.tolist()
        if include_types:
            allowed = set(include_types)
            ids = [i for i in ids if inst.loc[i, "asset_type"] in allowed]
        if exclude_st:
            ids = [i for i in ids if not bool(inst.loc[i, "is_st"])]
        if max_instruments:
            ids = ids[:max_instruments]
        return ids

    def bars_of(self, instrument_id: str, end: date | None = None) -> pd.DataFrame:
        bars = self.bars[self.bars["instrument_id"] == instrument_id]
        if end is not None:
            bars = bars[bars["trade_date"] <= pd.Timestamp(end)]
        return bars.sort_values("trade_date")

    def as_of(self, day: date, strict_pit: bool = True) -> "Dataset":
        """截取决策时点可见的数据；strict_pit=True 时同时按 available_at 过滤。"""
        cutoff = pd.Timestamp(day) + pd.Timedelta(hours=23, minutes=59, seconds=59)
        bars = self.bars[self.bars["trade_date"] <= pd.Timestamp(day)]
        if strict_pit and "available_at" in bars.columns:
            bars = bars[bars["available_at"].notna() & (bars["available_at"] <= cutoff)]
        fundamentals = self.fundamentals
        if fundamentals is not None and not fundamentals.empty:
            col = "available_at"
            fundamentals = fundamentals[fundamentals[col] <= cutoff]
        actions = self.corporate_actions
        if actions is not None and not actions.empty:
            actions = actions[actions["ex_date"] <= pd.Timestamp(day)]
        macro = self.macro
        if macro is not None and not macro.empty:
            macro = macro[macro["available_at"] <= cutoff]
        benchmark = self.benchmark
        if benchmark is not None:
            benchmark = benchmark[benchmark.index <= pd.Timestamp(day)]
        return Dataset(
            instruments=self.instruments,
            bars=bars.reset_index(drop=True),
            calendar=self.calendar,
            fundamentals=fundamentals.reset_index(drop=True),
            corporate_actions=actions.reset_index(drop=True),
            macro=macro.reset_index(drop=True) if macro is not None else macro,
            benchmark=benchmark,
            source=self.source,
        )

    # ---------- 面板 ----------
    def price_panel(self, field: str = "close", adjusted: bool = False) -> pd.DataFrame:
        bars = self.bars
        if bars.empty:
            return pd.DataFrame()
        value = bars[field]
        if adjusted and field in {"open", "high", "low", "close"}:
            value = value * bars["adj_factor"].fillna(1.0)
        panel = (
            pd.DataFrame({"trade_date": bars["trade_date"], "instrument_id": bars["instrument_id"], "v": value})
            .pivot(index="trade_date", columns="instrument_id", values="v")
            .sort_index()
        )
        return panel

    def volume_panel(self) -> pd.DataFrame:
        bars = self.bars
        if bars.empty:
            return pd.DataFrame()
        return bars.pivot(index="trade_date", columns="instrument_id", values="volume").sort_index()

    def returns_panel(self, adjusted: bool = True, min_periods: int = 1) -> pd.DataFrame:
        prices = self.price_panel("close", adjusted=adjusted)
        return prices.pct_change(fill_method=None)

    def adv_panel(self, window: int = 20) -> pd.DataFrame:
        bars = self.bars.copy()
        pivot = bars.pivot_table(index="trade_date", columns="instrument_id", values="amount", aggfunc="sum")
        return pivot.rolling(window, min_periods=max(5, window // 4)).mean()

    def fundamentals_asof(self, day: date, max_age_days: int | None = 400) -> pd.DataFrame:
        """返回决策时点可用的最新一期财务数据（point-in-time）。"""
        fund = self.fundamentals
        if fund is None or fund.empty:
            return pd.DataFrame()
        cutoff = pd.Timestamp(day) + pd.Timedelta(hours=23, minutes=59, seconds=59)
        available = fund[fund["available_at"] <= cutoff].sort_values("available_at")
        latest = available.groupby("instrument_id").tail(1).set_index("instrument_id")
        if max_age_days is not None and not latest.empty:
            stale = latest["available_at"] < (cutoff - pd.Timedelta(days=max_age_days))
            latest = latest[~stale.fillna(True)]
        return latest
