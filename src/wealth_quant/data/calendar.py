"""交易日历。

生产环境应接入交易所官方交易日历（上交所/深交所公告）。本模块提供：
- 由行情数据反推日历（与数据天然一致）；
- 内置示例节假日表，用于生成离线演示数据。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Sequence

import pandas as pd

# 示例节假日（2021-2026，主要法定休市日）。仅用于演示数据生成与离线测试，
# 实盘前必须替换为交易所官方日历。
CN_HOLIDAYS: frozenset[date] = frozenset(
    date(y, m, d)
    for y, m, d in [
        # 2021
        (2021, 1, 1), (2021, 2, 11), (2021, 2, 12), (2021, 2, 15), (2021, 2, 16), (2021, 2, 17),
        (2021, 4, 5), (2021, 5, 3), (2021, 6, 14), (2021, 9, 20), (2021, 9, 21), (2021, 10, 1),
        (2021, 10, 4), (2021, 10, 5), (2021, 10, 6), (2021, 10, 7),
        # 2022
        (2022, 1, 3), (2022, 1, 31), (2022, 2, 1), (2022, 2, 2), (2022, 2, 3), (2022, 2, 4),
        (2022, 4, 4), (2022, 4, 5), (2022, 5, 2), (2022, 5, 3), (2022, 5, 4), (2022, 6, 3),
        (2022, 9, 12), (2022, 10, 3), (2022, 10, 4), (2022, 10, 5), (2022, 10, 6), (2022, 10, 7),
        # 2023
        (2023, 1, 2), (2023, 1, 23), (2023, 1, 24), (2023, 1, 25), (2023, 1, 26), (2023, 1, 27),
        (2023, 4, 5), (2023, 5, 1), (2023, 5, 2), (2023, 5, 3), (2023, 6, 22), (2023, 6, 23),
        (2023, 9, 29), (2023, 10, 2), (2023, 10, 3), (2023, 10, 4), (2023, 10, 5), (2023, 10, 6),
        # 2024
        (2024, 1, 1), (2024, 2, 9), (2024, 2, 12), (2024, 2, 13), (2024, 2, 14), (2024, 2, 15),
        (2024, 2, 16), (2024, 4, 4), (2024, 4, 5), (2024, 5, 1), (2024, 5, 2), (2024, 5, 3),
        (2024, 6, 10), (2024, 9, 16), (2024, 9, 17), (2024, 10, 1), (2024, 10, 2), (2024, 10, 3),
        (2024, 10, 4), (2024, 10, 7),
        # 2025
        (2025, 1, 1), (2025, 1, 27), (2025, 1, 28), (2025, 1, 29), (2025, 1, 30), (2025, 1, 31),
        (2025, 2, 3), (2025, 2, 4), (2025, 4, 4), (2025, 5, 1), (2025, 5, 2), (2025, 5, 5),
        (2025, 6, 2), (2025, 10, 1), (2025, 10, 2), (2025, 10, 3), (2025, 10, 6), (2025, 10, 7),
        (2025, 10, 8),
        # 2026
        (2026, 1, 1), (2026, 1, 2), (2026, 2, 16), (2026, 2, 17), (2026, 2, 18), (2026, 2, 19),
        (2026, 2, 20), (2026, 4, 6), (2026, 5, 1), (2026, 5, 4), (2026, 5, 5), (2026, 6, 19),
        (2026, 9, 25), (2026, 10, 1), (2026, 10, 2), (2026, 10, 5), (2026, 10, 6), (2026, 10, 7),
    ]
)


def weekdays_excluding_holidays(start: date, end: date, holidays: Iterable[date] = CN_HOLIDAYS) -> list[date]:
    """生成 [start, end] 内的工作日（剔除周末与节假日）。"""
    holiday_set = set(holidays)
    days: list[date] = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5 and cur not in holiday_set:
            days.append(cur)
        cur += timedelta(days=1)
    return days


@dataclass(frozen=True)
class TradingCalendar:
    """不可变交易日历，支持按交易日偏移。"""

    dates: tuple[date, ...]

    def __post_init__(self) -> None:
        if not self.dates:
            raise ValueError("交易日历不能为空")
        ordered = tuple(sorted(set(self.dates)))
        object.__setattr__(self, "dates", ordered)

    # ---------- 构造 ----------
    @classmethod
    def from_series(cls, series: Sequence[date | str | pd.Timestamp]) -> "TradingCalendar":
        dates = tuple(pd.Timestamp(d).date() for d in series)
        return cls(dates)

    @classmethod
    def from_frame(cls, frame: pd.DataFrame, column: str = "trade_date") -> "TradingCalendar":
        if column not in frame.columns:
            raise KeyError(f"交易日历缺少列: {column}")
        return cls.from_series(frame[column].tolist())

    @classmethod
    def demo(cls, start: date = date(2021, 1, 4), end: date = date(2026, 6, 30)) -> "TradingCalendar":
        return cls(tuple(weekdays_excluding_holidays(start, end)))

    # ---------- 查询 ----------
    def __len__(self) -> int:
        return len(self.dates)

    def __contains__(self, day: date) -> bool:
        return day in self._index

    @property
    def _index(self) -> dict[date, int]:
        cached = getattr(self, "_idx_cache", None)
        if cached is None:
            cached = {d: i for i, d in enumerate(self.dates)}
            object.__setattr__(self, "_idx_cache", cached)
        return cached

    @property
    def first(self) -> date:
        return self.dates[0]

    @property
    def last(self) -> date:
        return self.dates[-1]

    def is_trading_day(self, day: date) -> bool:
        return day in self._index

    def index_of(self, day: date) -> int:
        try:
            return self._index[day]
        except KeyError as exc:
            raise ValueError(f"{day} 不是交易日") from exc

    def next(self, day: date, n: int = 1) -> date:
        """day 之后第 n 个交易日；若 day 不是交易日，先取其后第一个交易日。"""
        if day in self._index:
            idx = self._index[day] + n
        else:
            later = [d for d in self.dates if d > day]
            if not later:
                raise IndexError(f"{day} 之后没有交易日")
            idx = self._index[later[0]] + (n - 1)
        if idx < 0 or idx >= len(self.dates):
            raise IndexError(f"交易日偏移越界: {day} + {n}")
        return self.dates[idx]

    def prev(self, day: date, n: int = 1) -> date:
        if day in self._index:
            idx = self._index[day] - n
        else:
            earlier = [d for d in self.dates if d < day]
            if not earlier:
                raise IndexError(f"{day} 之前没有交易日")
            idx = self._index[earlier[-1]] - (n - 1)
        if idx < 0:
            raise IndexError(f"交易日偏移越界: {day} - {n}")
        return self.dates[idx]

    def range(self, start: date, end: date) -> list[date]:
        return [d for d in self.dates if start <= d <= end]

    def last_trading_day_of_month(self, year: int, month: int) -> date:
        days = [d for d in self.dates if d.year == year and d.month == month]
        if not days:
            raise ValueError(f"{year}-{month:02d} 无交易日")
        return days[-1]

    def is_month_end(self, day: date) -> bool:
        try:
            nxt = self.next(day)
        except IndexError:
            return True
        return nxt.month != day.month

    def is_week_end(self, day: date) -> bool:
        try:
            nxt = self.next(day)
        except IndexError:
            return True
        return nxt.isocalendar()[:2] != day.isocalendar()[:2]

    def trading_days_between(self, start: date, end: date) -> int:
        """(start, end] 之间的交易日数量，用于数据陈旧度判断。"""
        return sum(1 for d in self.dates if start < d <= end)
