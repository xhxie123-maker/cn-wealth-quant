"""历史回测引擎。

真实性要求（对应建设手册模块五）：
- 信号在收盘后形成，下一交易日开盘成交，绝不使用当日收盘价成交；
- 计入佣金、印花税、过户费、滑点与市场冲击；
- 模拟 T+1 可卖、涨跌停无法成交、停牌无法成交、整手约束、现金约束；
- 处理现金分红、拆股与退市；
- 风控引擎在每个调仓日复核目标仓位，被否决的调整不进入回测。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable

import numpy as np
import pandas as pd

from ..data.calendar import TradingCalendar
from ..data.quality import run_quality_checks
from ..data.rules import TradingRuleBook
from ..data.schema import Dataset
from ..risk.limits import RiskEngine
from ..risk.metrics import summarize
from ..strategies.base import StrategyContext, TargetPortfolio
from .costs import CostModel


@dataclass
class BacktestConfig:
    start: date
    end: date
    initial_cash: float = 1_000_000.0
    rebalance_frequency: str = "monthly"
    lot_size_default: int = 100
    min_trade_notional: float = 1000.0
    cash_buffer: float = 0.02
    risk_free_rate: float = 0.015
    execution: str = "next_open"
    benchmark: str = "000300.SH"


@dataclass
class Position:
    instrument_id: str
    shares: float = 0.0
    cost_total: float = 0.0
    sellable: float = 0.0

    @property
    def avg_cost(self) -> float:
        return self.cost_total / self.shares if self.shares > 0 else 0.0


@dataclass
class OrderIntent:
    instrument_id: str
    side: str
    shares: float
    target_weight: float
    reason: str
    created_on: date


@dataclass
class BacktestResult:
    nav: pd.Series
    benchmark: pd.Series | None
    fills: pd.DataFrame
    rejected: pd.DataFrame
    metrics: dict[str, Any]
    risk_states: pd.Series
    targets: list[dict[str, Any]] = field(default_factory=list)
    positions: pd.DataFrame | None = None
    strategy_id: str = ""
    strategy_version: str = ""
    config: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "metrics": {k: (None if isinstance(v, float) and not np.isfinite(v) else v)
                        for k, v in self.metrics.items()},
            "trades": int(len(self.fills)),
            "rejected_orders": int(len(self.rejected)),
            "final_nav": float(self.nav.iloc[-1]) if len(self.nav) else None,
            "start": str(self.nav.index[0].date()) if len(self.nav) else None,
            "end": str(self.nav.index[-1].date()) if len(self.nav) else None,
            "config": self.config,
        }


def _rebalance_days(dates: list[date], calendar: TradingCalendar, frequency: str) -> set[date]:
    if frequency == "daily":
        return set(dates)
    if frequency == "weekly":
        return {d for d in dates if calendar.is_week_end(d)}
    if frequency == "quarterly":
        return {d for d in dates if calendar.is_month_end(d) and d.month in (3, 6, 9, 12)}
    return {d for d in dates if calendar.is_month_end(d)}


def _round_lot(shares: float, lot: int) -> float:
    if shares <= 0:
        return 0.0
    return math.floor(shares / lot) * lot


class Backtester:
    def __init__(
        self,
        dataset: Dataset,
        strategy: Any,
        config: BacktestConfig,
        cost_model: CostModel | None = None,
        rulebook: TradingRuleBook | None = None,
        risk_engine: RiskEngine | None = None,
        risk_policy: dict[str, Any] | None = None,
    ):
        self.dataset = dataset
        self.strategy = strategy
        self.config = config
        self.cost_model = cost_model or CostModel()
        self.rulebook = rulebook or TradingRuleBook.with_2026_revision()
        self.risk_engine = risk_engine or (RiskEngine(risk_policy) if risk_policy else None)
        self.dates = dataset.calendar.range(config.start, config.end)
        if not self.dates:
            raise ValueError(f"回测区间内没有交易日: {config.start} - {config.end}")

        self.instrument_ids = dataset.instrument_ids
        self.sector_map = {i: str(dataset.instruments.loc[i, "industry"]) for i in self.instrument_ids}
        self.asset_types = {i: str(dataset.instruments.loc[i, "asset_type"]) for i in self.instrument_ids}

        self._prepare_panels()

    # ---------------- 预处理 ----------------
    def _prepare_panels(self) -> None:
        ds = self.dataset
        self.open = ds.price_panel("open", adjusted=False)
        self.close = ds.price_panel("close", adjusted=False)
        self.volume = ds.volume_panel()
        self.adv = ds.adv_panel(window=20).shift(1)
        self.prev_close = self.close.shift(1)

        idx_dates = pd.to_datetime(pd.Series(self.dates))
        self.open = self.open.reindex(self.open.index.union(idx_dates)).sort_index().ffill()
        self.close = self.close.reindex(self.close.index.union(idx_dates)).sort_index().ffill()
        self.volume = self.volume.reindex(self.volume.index.union(idx_dates)).sort_index()
        self.adv = self.adv.reindex(self.adv.index.union(idx_dates)).sort_index().ffill()
        self.prev_close = self.prev_close.reindex(self.prev_close.index.union(idx_dates)).sort_index().ffill()

        self._limits = self._build_limit_table()
        self._lots = {i: self._lot_size(i) for i in self.instrument_ids}
        self._bars = ds.bars.set_index(["trade_date", "instrument_id"]).sort_index()

        self.benchmark = None
        if ds.benchmark is not None and len(ds.benchmark):
            bench = ds.benchmark.copy()
            bench.index = pd.to_datetime(bench.index)
            self.benchmark = bench.reindex(self.close.index).ffill()

    def _lot_size(self, iid: str) -> int:
        inst = self.dataset.instrument(iid)
        rule = self.rulebook.rule_for(inst, self.dates[0])
        return int(rule.lot_size)

    def _build_limit_table(self) -> pd.DataFrame:
        """按规则生效日期分段，避免逐日解析规则（性能与可追溯性兼顾）。"""
        rows: dict[str, pd.Series] = {}
        date_index = pd.DatetimeIndex(pd.to_datetime(pd.Series(self.dates)).to_numpy())
        for iid in self.instrument_ids:
            inst = self.dataset.instrument(iid)
            boundaries = self.rulebook.effective_dates_for(inst)
            values = pd.Series(np.nan, index=date_index, dtype=float)
            for b_idx, start_d in enumerate(boundaries):
                end_d = boundaries[b_idx + 1] if b_idx + 1 < len(boundaries) else date(2100, 1, 1)
                mask = (date_index >= pd.Timestamp(start_d)) & (date_index < pd.Timestamp(end_d))
                if mask.any():
                    pct = self.rulebook.rule_for(inst, max(start_d, self.dates[0])).price_limit_pct
                    values.iloc[np.flatnonzero(mask)] = pct
            rows[iid] = values
        return pd.DataFrame(rows).reindex(date_index)

    def _limit_prices(self, day: date, iid: str) -> tuple[float, float]:
        prev = self.prev_close.at[pd.Timestamp(day), iid] if iid in self.prev_close.columns else np.nan
        pct = self._limits.at[pd.Timestamp(day), iid] if iid in self._limits.columns else np.nan
        if not np.isfinite(prev) or not np.isfinite(pct) or pct <= 0:
            return (0.0, float("inf"))
        return (round(prev * (1 - pct) + 1e-9, 2), round(prev * (1 + pct) + 1e-9, 2))

    def _bar(self, day: date, iid: str) -> pd.Series | None:
        try:
            row = self._bars.loc[(pd.Timestamp(day), iid)]
        except KeyError:
            return None
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        return row

    # ---------------- 主循环 ----------------
    def run(self) -> BacktestResult:
        cash = float(self.config.initial_cash)
        positions: dict[str, Position] = {}
        nav_dates: list[pd.Timestamp] = []
        nav_values: list[float] = []
        risk_states: list[str] = []
        fills: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        targets: list[dict[str, Any]] = []
        pending: list[OrderIntent] = []
        rebalance_set = _rebalance_days(self.dates, self.dataset.calendar, self.config.rebalance_frequency)
        position_rows: list[dict[str, Any]] = []

        for day in self.dates:
            ts = pd.Timestamp(day)

            # 1) 开盘前：公司行为（拆股、分红）
            cash = self._apply_corporate_actions(day, positions, cash, fills)

            # 2) 开盘：执行昨日收盘生成的委托
            if pending:
                cash, positions, new_fills, new_rejects = self._execute(day, pending, positions, cash)
                fills.extend(new_fills)
                rejected.extend(new_rejects)
                pending = []

            # 3) 收盘估值
            nav = cash + self._positions_value(day, positions)
            nav_dates.append(ts)
            nav_values.append(nav)

            # 4) 收盘后：生成下一交易日委托（仅调仓日）
            if day in rebalance_set and nav > 0:
                orders, target_meta = self._rebalance(day, positions, cash, nav, nav_values, nav_dates)
                pending = orders
                targets.append(target_meta)

            # 5) 日终：次日起全部持仓可卖（T+1 生效；T+0 同样可卖）
            for pos in positions.values():
                pos.sellable = pos.shares

            # 6) 记录持仓快照
            if positions:
                for iid, pos in positions.items():
                    price = self.close.at[ts, iid] if iid in self.close.columns else np.nan
                    position_rows.append({
                        "date": ts, "instrument_id": iid, "shares": pos.shares,
                        "price": float(price) if np.isfinite(price) else np.nan,
                        "value": float(pos.shares * price) if np.isfinite(price) else np.nan,
                        "weight": float(pos.shares * price / nav) if np.isfinite(price) and nav > 0 else 0.0,
                    })

        nav_series = pd.Series(nav_values, index=pd.DatetimeIndex(nav_dates), name="nav")
        bench = self.benchmark.reindex(nav_series.index).ffill() if self.benchmark is not None else None
        if bench is not None and len(bench):
            bench = bench / bench.dropna().iloc[0] * nav_series.iloc[0]

        fills_df = pd.DataFrame(fills)
        rejected_df = pd.DataFrame(rejected)
        positions_df = pd.DataFrame(position_rows)
        metrics = summarize(nav_series, bench, fills_df, self.config.risk_free_rate)
        metrics.update(self._extra_metrics(nav_series, fills_df))

        return BacktestResult(
            nav=nav_series, benchmark=bench, fills=fills_df, rejected=rejected_df, metrics=metrics,
            risk_states=pd.Series(risk_states or ["GREEN"] * len(nav_series), index=nav_series.index),
            targets=targets, positions=positions_df,
            strategy_id=getattr(self.strategy, "strategy_id", ""),
            strategy_version=getattr(self.strategy, "version", ""),
            config={
                "start": self.config.start.isoformat(), "end": self.config.end.isoformat(),
                "initial_cash": self.config.initial_cash,
                "rebalance_frequency": self.config.rebalance_frequency,
                "execution": self.config.execution,
                "costs": self.cost_model.__dict__,
            },
        )

    # ---------------- 公司行为 ----------------
    def _apply_corporate_actions(self, day: date, positions: dict[str, Position], cash: float,
                                 fills: list[dict[str, Any]]) -> float:
        actions = self.dataset.corporate_actions
        if actions is None or actions.empty:
            return cash
        today = actions[actions["ex_date"] == pd.Timestamp(day)]
        if today.empty:
            return cash
        for _, action in today.iterrows():
            iid = str(action["instrument_id"])
            pos = positions.get(iid)
            if pos is None or pos.shares <= 0:
                continue
            if action["action_type"] == "split":
                ratio = float(action["value"])
                if ratio > 0:
                    pos.shares *= ratio
                    pos.sellable *= ratio
                    fills.append({
                        "date": pd.Timestamp(day), "instrument_id": iid, "side": "SPLIT",
                        "shares": pos.shares, "price": 0.0, "notional": 0.0,
                        "commission": 0.0, "stamp_duty": 0.0, "transfer_fee": 0.0,
                        "slippage_cost": 0.0, "market_impact_cost": 0.0, "total_cost": 0.0,
                        "realized_pnl": np.nan, "reason": f"拆股 {ratio:g}:1",
                    })
            elif action["action_type"] == "cash_dividend":
                dps = float(action["value"])
                amount = pos.shares * dps
                cash += amount
                fills.append({
                    "date": pd.Timestamp(day), "instrument_id": iid, "side": "DIVIDEND",
                    "shares": pos.shares, "price": dps, "notional": amount,
                    "commission": 0.0, "stamp_duty": 0.0, "transfer_fee": 0.0,
                    "slippage_cost": 0.0, "market_impact_cost": 0.0, "total_cost": 0.0,
                    "realized_pnl": np.nan, "reason": "现金分红",
                })
        return cash

    # ---------------- 执行 ----------------
    def _execute(self, day: date, orders: list[OrderIntent], positions: dict[str, Position], cash: float):
        fills: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        # 先卖后买，释放现金
        ordered = [o for o in orders if o.side == "SELL"] + [o for o in orders if o.side == "BUY"]

        for order in ordered:
            iid = order.instrument_id
            bar = self._bar(day, iid)
            if bar is None or not np.isfinite(bar.get("open", np.nan)) or float(bar.get("open", 0)) <= 0:
                rejected.append(self._reject(day, order, "no_bar", "当日无行情"))
                continue
            if bool(bar.get("is_suspended", False)) or float(bar.get("volume", 0)) <= 0:
                rejected.append(self._reject(day, order, "suspended", "停牌或无成交"))
                continue

            open_px = float(bar["open"])
            down, up = self._limit_prices(day, iid)
            if order.side == "BUY" and open_px >= up - 0.005:
                rejected.append(self._reject(day, order, "limit_up", f"开盘 {open_px} 触及涨停 {up}，无法买入"))
                continue
            if order.side == "SELL" and open_px <= down + 0.005:
                rejected.append(self._reject(day, order, "limit_down", f"开盘 {open_px} 触及跌停 {down}，无法卖出"))
                continue

            adv = self.adv.at[pd.Timestamp(day), iid] if iid in self.adv.columns else np.nan
            adv = float(adv) if np.isfinite(adv) else None
            lot = self._lots.get(iid, self.config.lot_size_default)
            is_stock = self.asset_types.get(iid) == "stock"
            shares = float(order.shares)
            if order.side == "BUY":
                shares = _round_lot(shares, lot)
            if shares <= 0:
                continue

            pos = positions.setdefault(iid, Position(iid))
            if order.side == "SELL":
                shares = min(shares, pos.sellable)
                if shares <= 0:
                    rejected.append(self._reject(day, order, "t_plus_1", "T+1 限制：当日买入股份不可卖出"))
                    continue
                # 卖出可以不是整手（清仓零股允许），但保留整手更贴近常规
                if shares >= pos.shares - 1e-9:
                    shares = pos.shares
            else:
                max_by_adv = self.cost_model.max_tradable_shares(adv, open_px)
                if max_by_adv is not None:
                    shares = min(shares, _round_lot(max_by_adv, lot))
                # 现金约束（含费用）
                price_est = self.cost_model.execution_price("BUY", open_px, shares * open_px, adv)
                fee_est = self.cost_model.commission(shares * price_est) + self.cost_model.transfer_fee(shares * price_est)
                affordable = (cash - fee_est) / price_est if price_est > 0 else 0
                if shares * price_est + fee_est > cash:
                    shares = _round_lot(min(shares, affordable), lot)
                if shares <= 0:
                    rejected.append(self._reject(day, order, "insufficient_cash", "现金不足"))
                    continue

            notional_ref = shares * open_px
            exec_price = self.cost_model.execution_price(order.side, open_px, notional_ref, adv)
            notional = shares * exec_price
            costs = self.cost_model.total_cost(notional_ref, order.side, is_stock, adv)
            fees = costs["commission"] + costs["stamp_duty"] + costs["transfer_fee"]

            realized_pnl = np.nan
            if order.side == "BUY":
                total_out = notional + fees
                if total_out > cash + 1e-6:
                    # 最后一道现金闸门
                    shares = _round_lot(shares * 0.98, lot)
                    if shares <= 0:
                        rejected.append(self._reject(day, order, "insufficient_cash", "现金不足（含费用）"))
                        continue
                    notional_ref = shares * open_px
                    exec_price = self.cost_model.execution_price("BUY", open_px, notional_ref, adv)
                    notional = shares * exec_price
                    costs = self.cost_model.total_cost(notional_ref, "BUY", is_stock, adv)
                    fees = costs["commission"] + costs["stamp_duty"] + costs["transfer_fee"]
                    total_out = notional + fees
                cash -= total_out
                pos.shares += shares
                pos.cost_total += total_out
                pos.sellable -= 0  # 当日买入不可卖，sellable 在日终统一刷新
                pos.sellable = max(0.0, pos.sellable)
            else:
                proceeds = notional - fees
                cash += proceeds
                cost_part = pos.avg_cost * shares
                realized_pnl = proceeds - cost_part
                pos.shares -= shares
                pos.cost_total -= cost_part
                pos.sellable -= shares
                if pos.shares <= 1e-9:
                    pos.shares = 0.0
                    pos.cost_total = 0.0

            fills.append({
                "date": pd.Timestamp(day), "instrument_id": iid, "side": order.side,
                "shares": float(shares), "price": float(exec_price), "notional": float(notional),
                "commission": float(costs["commission"]), "stamp_duty": float(costs["stamp_duty"]),
                "transfer_fee": float(costs["transfer_fee"]),
                "slippage_cost": float(costs["slippage"]), "market_impact_cost": float(costs["market_impact"]),
                "total_cost": float(fees), "realized_pnl": realized_pnl,
                "reason": order.reason,
            })
        return cash, positions, fills, rejected

    @staticmethod
    def _reject(day: date, order: OrderIntent, code: str, message: str) -> dict[str, Any]:
        return {
            "date": pd.Timestamp(day), "instrument_id": order.instrument_id, "side": order.side,
            "shares": order.shares, "code": code, "message": message,
        }

    def _positions_value(self, day: date, positions: dict[str, Position]) -> float:
        value = 0.0
        ts = pd.Timestamp(day)
        for iid, pos in positions.items():
            if pos.shares <= 0:
                continue
            price = self.close.at[ts, iid] if iid in self.close.columns else np.nan
            if not np.isfinite(price):
                price = self.close[iid].ffill().loc[:ts].iloc[-1] if iid in self.close.columns else 0.0
            value += pos.shares * float(price)
        return value

    # ---------------- 调仓 ----------------
    def _rebalance(self, day: date, positions: dict[str, Position], cash: float, nav: float,
                   nav_values: list[float], nav_dates: list[pd.Timestamp]):
        ctx_dataset = self.dataset.as_of(day, strict_pit=True)
        ctx = StrategyContext(
            dataset=ctx_dataset, as_of=day, universe=ctx_dataset.universe(
                include_types=ctx_dataset.instruments["asset_type"].unique().tolist(),
                exclude_st=True,
            ),
            benchmark=ctx_dataset.benchmark,
        )
        target: TargetPortfolio = self.strategy.generate(ctx)

        current_weights = pd.Series({
            iid: (pos.shares * float(self.close.at[pd.Timestamp(day), iid]) / nav)
            for iid, pos in positions.items()
            if pos.shares > 0 and iid in self.close.columns
            and np.isfinite(self.close.at[pd.Timestamp(day), iid])
        }, dtype=float)

        nav_series = pd.Series(nav_values, index=pd.DatetimeIndex(nav_dates), name="nav")
        quality = run_quality_checks(
            ctx_dataset, as_of=day,
            max_stale_days=int(self.config.__dict__.get("max_stale_days", 3) or 3),
        )
        correlation = ctx_dataset.returns_panel(adjusted=True).tail(252).corr()

        approved_weights = target.weights
        assessment_state = "GREEN"
        approval_ok = True
        reasons: list[str] = []
        if self.risk_engine is not None:
            decision = self.risk_engine.review_target(
                as_of=day,
                target_weights=target.weights,
                current_weights=current_weights,
                sector_map=self.sector_map,
                nav_history=nav_series,
                quality=quality,
                nav_value=nav,
                portfolio_returns=None,
                correlation_matrix=correlation,
                strategy_id=target.strategy_id,
            )
            approved_weights = decision.approved_weights
            assessment_state = decision.assessment.state
            approval_ok = decision.approval is not None
            reasons = decision.reasons

        # 目标 -> 委托
        orders: list[OrderIntent] = []
        if approved_weights is not None and len(approved_weights):
            for iid in set(approved_weights.index) | set(current_weights.index):
                w_target = float(approved_weights.get(iid, 0.0))
                price = self.close.at[pd.Timestamp(day), iid] if iid in self.close.columns else np.nan
                if not np.isfinite(price) or price <= 0:
                    continue
                current_value = current_weights.get(iid, 0.0) * nav
                target_value = w_target * nav
                delta = target_value - current_value
                min_notional = max(self.config.min_trade_notional, 0.005 * nav)
                if abs(delta) < min_notional:
                    continue
                lot = self._lots.get(iid, self.config.lot_size_default)
                shares = _round_lot(abs(delta) / price, lot)
                if shares <= 0:
                    continue
                orders.append(OrderIntent(
                    instrument_id=iid, side="BUY" if delta > 0 else "SELL", shares=shares,
                    target_weight=w_target, reason="月度调仓" if self.config.rebalance_frequency == "monthly" else "定期调仓",
                    created_on=day,
                ))

        meta = {
            "date": day.isoformat(),
            "strategy_id": target.strategy_id,
            "strategy_version": target.version,
            "target_weights": {k: float(v) for k, v in target.weights.items()},
            "approved_weights": {k: float(v) for k, v in (
                approved_weights if approved_weights is not None else pd.Series(dtype=float)
            ).items()},
            "cash_weight": target.cash_weight,
            "risk_state": assessment_state,
            "risk_approved": bool(approval_ok),
            "risk_reasons": reasons,
            "orders": len(orders),
            "diagnostics": {k: v for k, v in target.diagnostics.items() if isinstance(v, (int, float, str, list))},
        }
        return orders, meta

    def _extra_metrics(self, nav: pd.Series, fills: pd.DataFrame) -> dict[str, Any]:
        years = max((len(nav) - 1) / 252, 1e-9)
        turnover = float("nan")
        if not fills.empty and "notional" in fills.columns:
            traded = fills[fills["side"].isin(["BUY", "SELL"])]["notional"].abs().sum()
            turnover = float(traded / nav.mean() / years)
        exposure = float("nan")
        return {"annualized_turnover": turnover, "average_nav": float(nav.mean())}
