"""数据质量检查：缺失、重复、异常价格、停牌一致性、陈旧度与 PIT 合规。

验收要求（来自建设手册）：交易标的关键数据无未处理的缺失与异常；
不使用决策时点之后才可获得的数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from .schema import Dataset

SEVERITY_ERROR = "ERROR"
SEVERITY_WARN = "WARN"


@dataclass
class QualityIssue:
    check: str
    severity: str
    message: str
    instrument_id: str | None = None
    count: int = 0
    samples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "severity": self.severity,
            "message": self.message,
            "instrument_id": self.instrument_id or "",
            "count": int(self.count),
            "samples": ";".join(self.samples[:5]),
        }


@dataclass
class QualityReport:
    as_of: date | None
    issues: list[QualityIssue]
    checked_rows: int
    instruments: int

    @property
    def errors(self) -> list[QualityIssue]:
        return [i for i in self.issues if i.severity == SEVERITY_ERROR]

    @property
    def warnings(self) -> list[QualityIssue]:
        return [i for i in self.issues if i.severity == SEVERITY_WARN]

    @property
    def passed(self) -> bool:
        return not self.errors

    def to_frame(self) -> pd.DataFrame:
        if not self.issues:
            return pd.DataFrame(columns=["check", "severity", "message", "instrument_id", "count", "samples"])
        return pd.DataFrame([i.to_dict() for i in self.issues])

    def summary(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "passed": self.passed,
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "checked_rows": self.checked_rows,
            "instruments": self.instruments,
        }


def _samples(series: pd.Series, limit: int = 5) -> list[str]:
    return [str(x) for x in series.head(limit).tolist()]


def run_quality_checks(
    dataset: Dataset,
    as_of: date | None = None,
    max_stale_days: int = 3,
    strict_pit: bool = True,
) -> QualityReport:
    issues: list[QualityIssue] = []
    bars = dataset.bars
    if bars is None or bars.empty:
        return QualityReport(as_of, [QualityIssue("bars_empty", SEVERITY_ERROR, "行情数据为空")], 0, 0)

    # 1) 重复
    dup = bars.duplicated(["instrument_id", "trade_date"], keep=False)
    if dup.any():
        issues.append(QualityIssue(
            "duplicate_bars", SEVERITY_ERROR, "存在同一资产同一交易日的重复记录",
            count=int(dup.sum()), samples=_samples(bars.loc[dup, "instrument_id"]),
        ))

    # 2) OHLC 逻辑一致性
    ohlc_bad = (
        (bars["high"] < bars[["open", "close"]].max(axis=1) - 1e-6)
        | (bars["low"] > bars[["open", "close"]].min(axis=1) + 1e-6)
        | (bars["high"] < bars["low"] - 1e-6)
    )
    if ohlc_bad.any():
        issues.append(QualityIssue(
            "ohlc_inconsistent", SEVERITY_ERROR, "最高/最低价与开收盘价矛盾",
            count=int(ohlc_bad.sum()), samples=_samples(bars.loc[ohlc_bad, "instrument_id"]),
        ))

    # 3) 非正价格 / 负成交量
    bad_price = (bars[["open", "high", "low", "close"]] <= 0).any(axis=1)
    if bad_price.any():
        issues.append(QualityIssue(
            "non_positive_price", SEVERITY_ERROR, "存在非正价格",
            count=int(bad_price.sum()), samples=_samples(bars.loc[bad_price, "instrument_id"]),
        ))
    bad_volume = bars["volume"].fillna(-1) < 0
    if bad_volume.any():
        issues.append(QualityIssue(
            "negative_volume", SEVERITY_ERROR, "成交量为负",
            count=int(bad_volume.sum()), samples=_samples(bars.loc[bad_volume, "instrument_id"]),
        ))

    # 4) 异常收益（超过涨跌停幅度 + 3 个百分点，视为可疑）
    for iid, group in bars.groupby("instrument_id"):
        prices = group.sort_values("trade_date").set_index("trade_date")["close"]
        ret = prices.pct_change(fill_method=None)
        if ret.abs().max(skipna=True) and ret.abs().max() > 0.32:
            outliers = ret[ret.abs() > 0.32]
            issues.append(QualityIssue(
                "extreme_return", SEVERITY_WARN, "单日涨跌幅异常，需核对除权或数据错误",
                instrument_id=iid, count=int(outliers.shape[0]),
                samples=[str(pd.Timestamp(x).date()) for x in outliers.index[:5]],
            ))

    # 5) 停牌一致性：成交量为 0 但价格发生变化
    susp = (bars["volume"].fillna(0) <= 0)
    if susp.any():
        changed = bars.loc[susp].copy()
        changed["prev_close"] = changed.groupby("instrument_id")["close"].shift(1)
        bad = (changed["prev_close"].notna()) & ((changed["close"] - changed["prev_close"]).abs() > 1e-6)
        if bad.any():
            issues.append(QualityIssue(
                "suspended_price_change", SEVERITY_WARN, "停牌日价格发生变化，需核对数据源",
                count=int(bad.sum()), samples=_samples(changed.loc[bad, "instrument_id"]),
            ))

    # 6) 复权因子单调性（除权只会让累计因子上升）
    for iid, group in bars.groupby("instrument_id"):
        factor = group.sort_values("trade_date")["adj_factor"]
        if (factor.diff() < -1e-9).any():
            issues.append(QualityIssue(
                "adj_factor_decrease", SEVERITY_ERROR, "复权因子出现回退，可能复权计算错误",
                instrument_id=iid, count=int((factor.diff() < -1e-9).sum()),
            ))

    # 7) 覆盖率（缺失交易日）
    calendar = dataset.calendar
    if calendar is not None and len(calendar):
        for iid, group in bars.groupby("instrument_id"):
            days = set(pd.to_datetime(group["trade_date"]).dt.date)
            expected = set(d for d in calendar.dates if (as_of is None or d <= as_of))
            missing = expected - days
            # 上市前/退市后不计入
            inst = dataset.instruments.loc[iid] if iid in dataset.instruments.index else None
            if inst is not None:
                if pd.notna(inst.get("list_date")):
                    missing = {d for d in missing if d >= pd.Timestamp(inst["list_date"]).date()}
                if pd.notna(inst.get("delist_date")):
                    missing = {d for d in missing if d <= pd.Timestamp(inst["delist_date"]).date()}
            if len(missing) > max(10, int(0.03 * len(expected))):
                issues.append(QualityIssue(
                    "missing_trading_days", SEVERITY_WARN, "缺失交易日超过 3%，需确认停牌或数据缺口",
                    instrument_id=iid, count=len(missing), samples=[str(d) for d in sorted(missing)[:5]],
                ))

    # 8) 数据陈旧度
    if calendar is not None and len(calendar):
        latest = pd.to_datetime(bars["trade_date"]).max().date()
        reference = as_of or latest
        stale_days = calendar.trading_days_between(latest, reference)
        if stale_days > max_stale_days:
            issues.append(QualityIssue(
                "stale_data", SEVERITY_ERROR,
                f"最新行情为 {latest}，距决策日 {reference} 已 {stale_days} 个交易日，超过阈值 {max_stale_days}",
                count=stale_days,
            ))

    # 9) PIT 合规
    if strict_pit:
        for col in ("available_at", "published_at"):
            if col in bars.columns and bars[col].isna().any():
                issues.append(QualityIssue(
                    "missing_timestamp", SEVERITY_ERROR, f"行情缺少 {col}，无法保证 point-in-time",
                    count=int(bars[col].isna().sum()),
                ))
        if {"available_at", "published_at"} <= set(bars.columns):
            bad = bars["available_at"] < bars["published_at"]
            if bad.any():
                issues.append(QualityIssue(
                    "available_before_published", SEVERITY_ERROR,
                    "available_at 早于 published_at，存在未来数据泄漏风险",
                    count=int(bad.sum()), samples=_samples(bars.loc[bad, "instrument_id"]),
                ))
        fund = dataset.fundamentals
        if fund is not None and not fund.empty and "available_at" in fund:
            bad_fund = fund["available_at"] < fund["published_at"]
            if bad_fund.any():
                issues.append(QualityIssue(
                    "fundamental_available_before_published", SEVERITY_ERROR,
                    "财务数据 available_at 早于 published_at",
                    count=int(bad_fund.sum()),
                ))

    return QualityReport(
        as_of=as_of,
        issues=issues,
        checked_rows=int(len(bars)),
        instruments=int(bars["instrument_id"].nunique()),
    )
