"""中国市场交易规则簿：按交易所 / 证券类别 / 板块 / 生效日期动态解析。

为什么必须版本化：
- 上交所、深交所 2026 年修订交易规则自 2026-07-06 起实施，交易参数不能硬编码；
- 股票 ETF 与债券 ETF、货币 ETF、黄金 ETF、跨境 ETF 的交收机制不同（T+1 / T+0）；
- 涨跌停幅度随板块（主板 / 创业板 / 科创板 / 北交所 / ST）不同。

本模块只负责“规则解析”，不负责行情判断。生产使用前必须用交易所正式规则核对，
未核对的规则以 verified=False 标记，并在决策报告中提示。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Any, Iterable

T0_ASSET_TYPES = {"bond_etf", "money_etf", "gold_etf", "crossborder_etf", "bond"}


@dataclass(frozen=True)
class TradingRule:
    exchange: str
    asset_type: str
    board: str
    t_plus: int                 # 1 = T+1，0 = T+0
    price_limit_pct: float      # 0 表示无涨跌停限制
    lot_size: int               # 最小交易单位（股/份/张）
    stamp_duty: bool            # 卖出是否征收印花税
    effective_from: date
    verified: bool = True
    note: str = ""

    def price_limits(self, prev_close: float) -> tuple[float, float]:
        """按昨收计算涨跌停价；A 股规则为四舍五入到 0.01 元。"""
        if self.price_limit_pct <= 0 or prev_close <= 0:
            return (0.0, float("inf"))
        up = round(prev_close * (1 + self.price_limit_pct) + 1e-9, 2)
        down = round(prev_close * (1 - self.price_limit_pct) + 1e-9, 2)
        return (down, up)


def _default_rules() -> list[TradingRule]:
    started = date(2019, 1, 1)
    rules = [
        # 股票：主板 ±10%，T+1，100 股整手，卖出印花税
        TradingRule("SSE", "stock", "main", 1, 0.10, 100, True, started),
        TradingRule("SZSE", "stock", "main", 1, 0.10, 100, True, started),
        # 创业板 / 科创板 ±20%
        TradingRule("SZSE", "stock", "chinext", 1, 0.20, 100, True, started),
        TradingRule("SSE", "stock", "star", 1, 0.20, 100, True, started),
        # 北交所 ±30%
        TradingRule("BSE", "stock", "bse", 1, 0.30, 100, True, started),
        # 股票 ETF：T+1，±10%，免印花税
        TradingRule("SSE", "etf", "etf", 1, 0.10, 100, False, started),
        TradingRule("SZSE", "etf", "etf", 1, 0.10, 100, False, started),
        # 债券 ETF / 货币 ETF：T+0，无涨跌停（以交易所公告为准）
        TradingRule("SSE", "bond_etf", "etf", 0, 0.0, 100, False, started, False,
                    "债券 ETF 支持 T+0；涨跌停安排需按具体产品公告核对"),
        TradingRule("SZSE", "bond_etf", "etf", 0, 0.0, 100, False, started, False,
                    "债券 ETF 支持 T+0；涨跌停安排需按具体产品公告核对"),
        TradingRule("SSE", "money_etf", "etf", 0, 0.0, 1, False, started, False,
                    "货币 ETF 支持 T+0，申赎与交易单位以产品公告为准"),
        # 黄金 ETF：T+0，±10%
        TradingRule("SSE", "gold_etf", "etf", 0, 0.10, 100, False, started, False,
                    "黄金 ETF 支持 T+0"),
        TradingRule("SZSE", "gold_etf", "etf", 0, 0.10, 100, False, started, False,
                    "黄金 ETF 支持 T+0"),
        # 跨境 ETF：T+0，±10%
        TradingRule("SSE", "crossborder_etf", "etf", 0, 0.10, 100, False, started, False,
                    "跨境 ETF 支持 T+0；额度与溢价风险需单独监控"),
        TradingRule("SZSE", "crossborder_etf", "etf", 0, 0.10, 100, False, started, False,
                    "跨境 ETF 支持 T+0"),
        # 交易所债券：T+0，无涨跌停，10 张/手
        TradingRule("SSE", "bond", "bond", 0, 0.0, 10, False, started, False,
                    "个人债券交易受适当性与账户权限约束，需单独核验"),
        TradingRule("SZSE", "bond", "bond", 0, 0.0, 10, False, started, False,
                    "个人债券交易受适当性与账户权限约束，需单独核验"),
    ]
    return rules


class TradingRuleBook:
    """规则簿：同一 (exchange, asset_type, board) 可有多条按生效日期排序的规则。"""

    def __init__(self, rules: Iterable[TradingRule] | None = None):
        self._rules: dict[tuple[str, str, str], list[TradingRule]] = {}
        for rule in (rules if rules is not None else _default_rules()):
            self._rules.setdefault((rule.exchange, rule.asset_type, rule.board), []).append(rule)
        for bucket in self._rules.values():
            bucket.sort(key=lambda r: r.effective_from)

    @classmethod
    def with_2026_revision(cls) -> "TradingRuleBook":
        """加入 2026-07-06 生效的规则占位版本，演示“按生效日期读取”的机制。

        说明：占位版本沿用既有参数，仅用于证明规则可按日期切换；正式参数必须依据
        上交所/深交所 2026 年修订规则原文校准后再标记 verified=True。
        """
        book = cls()
        placeholder_date = date(2026, 7, 6)
        for key, bucket in list(book._rules.items()):
            latest = bucket[-1]
            book._rules[key].append(
                replace(
                    latest,
                    effective_from=placeholder_date,
                    verified=False,
                    note=(latest.note + "；2026-07-06 修订版参数待按正式规则校准").strip("；"),
                )
            )
        return book

    def rule_for(self, instrument: Any, on: date) -> TradingRule:
        """解析某资产在指定日期的交易规则。

        instrument 支持 Instrument 数据类或含 asset_type/exchange/board/is_st 的映射。
        """
        if hasattr(instrument, "asset_type"):
            asset_type = instrument.asset_type
            exchange = instrument.exchange
            board = instrument.board
            is_st = bool(getattr(instrument, "is_st", False))
            instrument_id = getattr(instrument, "instrument_id", "?")
        else:
            asset_type = str(instrument.get("asset_type", "stock"))
            exchange = str(instrument.get("exchange", "SSE"))
            board = str(instrument.get("board", "main"))
            is_st = bool(instrument.get("is_st", False))
            instrument_id = str(instrument.get("instrument_id", "?"))

        candidates = self._rules.get((exchange, asset_type, board))
        if not candidates:
            # 资产类别兜底：先按类别，再按股票主板
            candidates = next(
                (bucket for (ex, at, _), bucket in self._rules.items() if at == asset_type and ex == exchange),
                None,
            )
        if not candidates:
            candidates = self._rules.get(("SSE", "stock", "main"))
        if not candidates:
            raise KeyError(f"找不到交易规则: {instrument_id} {exchange}/{asset_type}/{board}")

        chosen = None
        for rule in candidates:
            if rule.effective_from <= on:
                chosen = rule
        if chosen is None:
            chosen = candidates[0]

        # ST 股票在主板适用 ±5%
        if asset_type == "stock" and is_st and board == "main":
            chosen = replace(chosen, price_limit_pct=0.05, note=(chosen.note + "；ST 主板 ±5%").strip("；"))
        return chosen

    def describe(self, instrument: Any, on: date) -> dict[str, Any]:
        rule = self.rule_for(instrument, on)
        return {
            "exchange": rule.exchange,
            "asset_type": rule.asset_type,
            "board": rule.board,
            "t_plus": rule.t_plus,
            "price_limit_pct": rule.price_limit_pct,
            "lot_size": rule.lot_size,
            "stamp_duty": rule.stamp_duty,
            "effective_from": rule.effective_from.isoformat(),
            "verified": rule.verified,
            "note": rule.note,
        }

    def effective_dates_for(self, instrument: Any) -> list[date]:
        """返回该资产对应规则桶的所有生效日期，用于按日期分段读取参数。"""
        if hasattr(instrument, "asset_type"):
            key = (instrument.exchange, instrument.asset_type, instrument.board)
        else:
            key = (str(instrument.get("exchange", "SSE")), str(instrument.get("asset_type", "stock")),
                   str(instrument.get("board", "main")))
        bucket = self._rules.get(key)
        if bucket is None:
            bucket = next((b for k, b in self._rules.items() if k[1] == key[1] and k[0] == key[0]), None)
        if bucket is None:
            return [date(2019, 1, 1)]
        return [r.effective_from for r in bucket]
