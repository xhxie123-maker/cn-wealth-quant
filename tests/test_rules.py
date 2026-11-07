"""交易规则簿：板块涨跌幅、T+1/T+0、ST、按生效日期切换。"""

from __future__ import annotations

from datetime import date

from wealth_quant.data.rules import TradingRuleBook


def test_stock_main_t_plus_1_and_10pct():
    book = TradingRuleBook()
    rule = book.rule_for({"asset_type": "stock", "exchange": "SSE", "board": "main"}, date(2024, 1, 2))
    assert rule.t_plus == 1
    assert rule.price_limit_pct == 0.10
    assert rule.lot_size == 100
    assert rule.stamp_duty is True


def test_chinext_and_star_20pct():
    book = TradingRuleBook()
    gem = book.rule_for({"asset_type": "stock", "exchange": "SZSE", "board": "chinext"}, date(2024, 1, 2))
    star = book.rule_for({"asset_type": "stock", "exchange": "SSE", "board": "star"}, date(2024, 1, 2))
    assert gem.price_limit_pct == 0.20
    assert star.price_limit_pct == 0.20


def test_st_main_is_5pct():
    book = TradingRuleBook()
    rule = book.rule_for(
        {"asset_type": "stock", "exchange": "SSE", "board": "main", "is_st": True}, date(2024, 1, 2)
    )
    assert rule.price_limit_pct == 0.05


def test_etf_split_t_plus_1_vs_t_plus_0():
    book = TradingRuleBook()
    stock_etf = book.rule_for(
        {"asset_type": "etf", "exchange": "SSE", "board": "etf"}, date(2024, 1, 2)
    )
    bond_etf = book.rule_for(
        {"asset_type": "bond_etf", "exchange": "SSE", "board": "etf"}, date(2024, 1, 2)
    )
    money_etf = book.rule_for(
        {"asset_type": "money_etf", "exchange": "SSE", "board": "etf"}, date(2024, 1, 2)
    )
    assert stock_etf.t_plus == 1
    assert bond_etf.t_plus == 0
    assert money_etf.t_plus == 0
    assert stock_etf.stamp_duty is False


def test_2026_revision_switches_by_effective_date():
    book = TradingRuleBook.with_2026_revision()
    before = book.rule_for({"asset_type": "stock", "exchange": "SSE", "board": "main"}, date(2026, 7, 3))
    after = book.rule_for({"asset_type": "stock", "exchange": "SSE", "board": "main"}, date(2026, 7, 6))
    assert before.effective_from < after.effective_from
    assert after.verified is False, "2026-07-06 占位规则必须标记为未核验"
    assert date(2026, 7, 6) in book.effective_dates_for(
        {"asset_type": "stock", "exchange": "SSE", "board": "main"}
    )


def test_price_limits_round_to_cent():
    book = TradingRuleBook()
    rule = book.rule_for({"asset_type": "stock", "exchange": "SSE", "board": "main"}, date(2024, 1, 2))
    down, up = rule.price_limits(10.0)
    assert down == 9.0
    assert up == 11.0
