"""独立风控引擎：五级风险检查 + 一票否决 + 风险状态机 + 审批签名。

关键设计（对应建设手册模块六）：
- 风控参数与策略代码分离；
- 风控引擎可以否决策略建议，策略模块无法绕过；
- 状态分为 GREEN / YELLOW / RED / SYSTEM_HALT；
- 决策引擎必须持有风控签发的审批令牌才能输出可执行建议。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Mapping

import numpy as np
import pandas as pd

from ..data.quality import QualityReport
from ..portfolio.optimizer import project_weights
from .metrics import max_drawdown, var_es, concentration_hhi, portfolio_risk_contributions

GREEN, YELLOW, RED, HALT = "GREEN", "YELLOW", "RED", "SYSTEM_HALT"
_STATE_ORDER = {GREEN: 0, YELLOW: 1, RED: 2, HALT: 3}


@dataclass
class RiskCheck:
    code: str
    name: str
    level: str
    passed: bool
    value: float | str | None
    limit: float | str | None
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code, "name": self.name, "level": self.level, "passed": bool(self.passed),
            "value": self.value, "limit": self.limit, "message": self.message,
        }


@dataclass
class RiskAssessment:
    as_of: date
    state: str
    checks: list[RiskCheck] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    system_halt: bool = False

    @property
    def vetoes(self) -> list[RiskCheck]:
        return [c for c in self.checks if not c.passed]

    @property
    def passed(self) -> bool:
        return self.state in (GREEN, YELLOW)

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "state": self.state,
            "passed": self.passed,
            "system_halt": self.system_halt,
            "vetoes": [c.to_dict() for c in self.vetoes],
            "checks": [c.to_dict() for c in self.checks],
            "metrics": {k: (None if (isinstance(v, float) and not np.isfinite(v)) else v) for k, v in self.metrics.items()},
        }


@dataclass(frozen=True)
class RiskApproval:
    """风控签发的审批令牌。决策引擎必须校验签名，防止被伪造或跳过。"""

    as_of: str
    strategy_id: str
    weights_digest: str
    state: str
    issued_at: str
    nonce: str
    signature: str
    approved: bool


@dataclass
class RiskDecision:
    assessment: RiskAssessment
    approved_weights: pd.Series
    requested_weights: pd.Series
    rejected: bool
    reasons: list[str]
    approval: RiskApproval | None = None

    @property
    def state(self) -> str:
        return self.assessment.state


def _weights_digest(weights: pd.Series) -> str:
    payload = "|".join(f"{k}:{round(float(v), 8)}" for k, v in sorted(weights.items()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class RiskEngine:
    def __init__(self, policy: dict[str, Any], secret: bytes | None = None):
        self.policy = policy
        self._secret = secret or secrets.token_bytes(32)

    # ---------------- 配置读取 ----------------
    def _get(self, path: str, default: Any = None) -> Any:
        cur: Any = self.policy
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    # ---------------- 风险评估 ----------------
    def assess(
        self,
        as_of: date,
        nav_history: pd.Series | None = None,
        weights: pd.Series | None = None,
        sector_map: Mapping[str, str] | None = None,
        quality: QualityReport | None = None,
        target_weights: pd.Series | None = None,
        order_notional: float = 0.0,
        nav_value: float | None = None,
        portfolio_returns: pd.Series | None = None,
        correlation_matrix: pd.DataFrame | None = None,
    ) -> RiskAssessment:
        checks: list[RiskCheck] = []
        metrics: dict[str, float] = {}
        current = (weights if weights is not None else pd.Series(dtype=float)).astype(float)
        proposed = (target_weights if target_weights is not None else current).astype(float)
        nav_series = nav_history if nav_history is not None else pd.Series(dtype=float)
        nav = float(nav_value) if nav_value is not None else (float(nav_series.iloc[-1]) if len(nav_series) else 0.0)

        # ---- 第五级：系统风险 / 数据 ----
        system_halt = False
        if quality is not None:
            stale_codes = {"stale_data", "missing_timestamp", "available_before_published",
                           "fundamental_available_before_published", "duplicate_bars", "ohlc_inconsistent"}
            severe = [i for i in quality.errors if i.check in stale_codes]
            if severe and bool(self._get("data_protection.reject_stale_data", True)):
                system_halt = True
                checks.append(RiskCheck(
                    "data_integrity", "数据完整性", HALT, False, len(severe),
                    "无", f"数据存在 {len(severe)} 项严重问题，停止生成可执行建议",
                ))
            else:
                checks.append(RiskCheck("data_integrity", "数据完整性", GREEN, True, len(quality.errors), 0, "数据质量检查通过"))

        # ---- 第一级：单资产 / 集中度 ----
        max_single = float(self._get("position_limits.max_single_asset_weight", 0.20))
        if len(proposed):
            over = proposed[proposed > max_single + 1e-12]
            checks.append(RiskCheck(
                "single_asset_weight", "单资产权重", YELLOW if len(over) else GREEN, len(over) == 0,
                float(proposed.max()), max_single,
                "存在超过单资产上限的标的，将被裁剪" if len(over) else "单资产权重在限内",
            ))
            hhi = concentration_hhi(proposed)
            metrics["hhi"] = hhi
            max_hhi = float(self._get("concentration.max_hhi", 0.30))
            checks.append(RiskCheck(
                "concentration_hhi", "集中度 HHI", YELLOW if hhi > max_hhi else GREEN, hhi <= max_hhi,
                hhi, max_hhi, "组合集中度偏高" if hhi > max_hhi else "集中度正常",
            ))
            max_n = int(self._get("concentration.max_assets_above_5pct", 8))
            count5 = int((proposed >= 0.05).sum())
            checks.append(RiskCheck(
                "assets_above_5pct", "5% 以上持仓数量", YELLOW if count5 > max_n else GREEN, count5 <= max_n,
                count5, max_n, "超过 5% 的持仓数量过多" if count5 > max_n else "持仓分散度正常",
            ))

        # ---- 第二级：组合 / 行业 / 暴露 ----
        if sector_map is not None and len(proposed):
            sectors = pd.Series({k: sector_map.get(k, "未分类") for k in proposed.index})
            exposure = proposed.groupby(sectors).sum().sort_values(ascending=False)
            metrics["max_sector_weight"] = float(exposure.iloc[0]) if len(exposure) else 0.0
            max_sector = float(self._get("position_limits.max_sector_weight", 0.35))
            worst = float(exposure.iloc[0]) if len(exposure) else 0.0
            checks.append(RiskCheck(
                "sector_weight", "行业集中度", YELLOW if worst > max_sector else GREEN, worst <= max_sector,
                worst, max_sector, "行业集中度超过上限，将被裁剪" if worst > max_sector else "行业集中度正常",
            ))

        max_exposure = float(self._get("position_limits.max_portfolio_exposure", 0.98))
        total_exposure = float(proposed.sum())
        metrics["exposure"] = total_exposure
        checks.append(RiskCheck(
            "portfolio_exposure", "总仓位", RED if total_exposure > max_exposure + 1e-6 else GREEN,
            total_exposure <= max_exposure + 1e-6, total_exposure, max_exposure,
            "总仓位超过上限" if total_exposure > max_exposure + 1e-6 else "总仓位在限内",
        ))
        if bool(self._get("leverage.enabled", False)) is False:
            checks.append(RiskCheck(
                "leverage", "杠杆", RED if total_exposure > 1.0 + 1e-6 else GREEN, total_exposure <= 1.0 + 1e-6,
                total_exposure, 1.0, "系统禁止杠杆" if total_exposure > 1.0 else "无杠杆",
            ))

        # ---- 回撤控制 ----
        drawdown = 0.0
        if len(nav_series) > 1:
            # 用“当前回撤”（相对历史最高净值），而不是历史最大回撤：
            # 后者一旦创新高前触及阈值就会永久锁死 RED，净值恢复后也无法解除。
            nav_hist = pd.to_numeric(nav_series, errors="coerce").dropna()
            if len(nav_hist) > 1:
                drawdown = abs(float((nav_hist / nav_hist.cummax() - 1).iloc[-1]))
        metrics["drawdown"] = drawdown
        warn = float(self._get("drawdown_control.warning_threshold", 0.06))
        reduce_at = float(self._get("drawdown_control.reduce_risk_threshold", 0.10))
        emergency = float(self._get("drawdown_control.emergency_threshold", 0.15))
        if drawdown >= emergency:
            checks.append(RiskCheck("drawdown_emergency", "回撤-紧急", RED, False, drawdown, emergency,
                                    "回撤达到紧急阈值，需强制降仓至防御资产"))
        elif drawdown >= reduce_at:
            checks.append(RiskCheck("drawdown_reduce", "回撤-降险", RED, False, drawdown, reduce_at,
                                    "回撤达到降险阈值，暂停新增风险敞口"))
        elif drawdown >= warn:
            checks.append(RiskCheck("drawdown_warning", "回撤-关注", YELLOW, False, drawdown, warn,
                                    "回撤接近阈值，需检查仓位与信号"))
        else:
            checks.append(RiskCheck("drawdown", "回撤", GREEN, True, drawdown, warn, "回撤在正常范围"))

        # ---- 市场风险：VaR / ES / 波动 ----
        rets = portfolio_returns
        if rets is None and len(nav_series) > 2:
            rets = nav_series.pct_change(fill_method=None).dropna()
        if rets is not None and len(rets) > 20:
            var95, es95 = var_es(rets, 0.95)
            metrics["var_95"] = var95
            metrics["es_95"] = es95
            var_limit = float(self._get("market_risk.var_95_limit", 0.035))
            es_limit = float(self._get("market_risk.es_95_limit", 0.055))
            checks.append(RiskCheck(
                "var_95", "1日 95% VaR", RED if abs(var95) > var_limit else GREEN, abs(var95) <= var_limit,
                var95, -var_limit, "VaR 超过上限" if abs(var95) > var_limit else "VaR 在限内",
            ))
            checks.append(RiskCheck(
                "es_95", "1日 95% ES", RED if abs(es95) > es_limit else GREEN, abs(es95) <= es_limit,
                es95, -es_limit, "ES 超过上限" if abs(es95) > es_limit else "ES 在限内",
            ))
            if len(rets) >= 120:
                recent = float(rets.tail(20).std(ddof=0))
                base = float(rets.tail(120).std(ddof=0))
                ratio = recent / base if base > 0 else 1.0
                metrics["volatility_ratio"] = ratio
                limit = float(self._get("market_risk.volatility_spike_ratio", 1.8))
                checks.append(RiskCheck(
                    "volatility_spike", "波动率突升", YELLOW if ratio > limit else GREEN, ratio <= limit,
                    ratio, limit, "近期波动显著高于中期水平" if ratio > limit else "波动率平稳",
                ))

        # ---- 相关性 ----
        if correlation_matrix is not None and len(correlation_matrix) > 1:
            corr = correlation_matrix.reindex(index=proposed.index, columns=proposed.index).abs()
            mat = corr.to_numpy(dtype=float, copy=True)
            np.fill_diagonal(mat, 0.0)
            max_corr = float(np.nanmax(mat)) if mat.size else 0.0
            metrics["max_pairwise_correlation"] = max_corr
            limit = float(self._get("concentration.max_pairwise_correlation", 0.85))
            checks.append(RiskCheck(
                "correlation", "资产相关性", YELLOW if max_corr > limit else GREEN, max_corr <= limit,
                max_corr, limit, "存在高度相关资产，分散化不足" if max_corr > limit else "相关性正常",
            ))

        # ---- 第三级：交易风险 ----
        if nav > 0 and order_notional:
            turnover = float(order_notional / nav)
            metrics["turnover"] = turnover
            limit = float(self._get("trading_limits.max_daily_turnover", 0.30))
            checks.append(RiskCheck(
                "daily_turnover", "单日换手", RED if turnover > limit else GREEN, turnover <= limit,
                turnover, limit, "单日换手超过上限" if turnover > limit else "换手正常",
            ))
            max_order = float(self._get("trading_limits.max_order_size_cny", 200000))
            largest = float(order_notional)
            checks.append(RiskCheck(
                "order_size", "单笔委托额", YELLOW if largest > max_order * 3 else GREEN, True, largest, max_order,
                "单笔委托额较大，需拆分执行" if largest > max_order * 3 else "委托额正常",
            ))

        # ---- 状态机 ----
        levels = [c.level for c in checks if not c.passed]
        if system_halt or HALT in levels:
            state = HALT
        elif RED in levels:
            state = RED
        elif YELLOW in levels:
            state = YELLOW
        else:
            state = GREEN
        return RiskAssessment(as_of=as_of, state=state, checks=checks, metrics=metrics, system_halt=system_halt)

    # ---------------- 审批与裁剪 ----------------
    def review_target(
        self,
        as_of: date,
        target_weights: pd.Series,
        current_weights: pd.Series,
        sector_map: Mapping[str, str] | None = None,
        nav_history: pd.Series | None = None,
        quality: QualityReport | None = None,
        nav_value: float | None = None,
        portfolio_returns: pd.Series | None = None,
        correlation_matrix: pd.DataFrame | None = None,
        strategy_id: str = "",
        estimated_order_notional: float = 0.0,
    ) -> RiskDecision:
        """对策略目标仓位做风控复核，返回可执行权重与审批令牌。"""
        target = target_weights.astype(float).fillna(0.0)
        current = current_weights.reindex(target.index).fillna(0.0).astype(float) if len(current_weights) else pd.Series(0.0, index=target.index)
        current = current.reindex(current.index.union(target.index)).fillna(0.0)
        target = target.reindex(current.index).fillna(0.0)
        # 归一化处理（防止浮点误差）
        if target.sum() > 1.0:
            target = target / target.sum()

        max_single = float(self._get("position_limits.max_single_asset_weight", 0.20))
        max_sector = float(self._get("position_limits.max_sector_weight", 0.35))
        max_exposure = float(self._get("position_limits.max_portfolio_exposure", 0.98))
        # 先做结构约束投影（单资产/行业/总仓位），再评估风险状态。
        # 顺序很重要：否则“目标权重超上限”会被误判成 RED 一票否决，而它其实只是需要裁剪。
        adjusted = project_weights(target, max_weight=max_single, sector_map=sector_map, max_sector=max_sector,
                                   max_total=max_exposure)

        assessment = self.assess(
            as_of=as_of, nav_history=nav_history, weights=adjusted, sector_map=sector_map,
            quality=quality, target_weights=adjusted, order_notional=estimated_order_notional,
            nav_value=nav_value, portfolio_returns=portfolio_returns,
            correlation_matrix=correlation_matrix,
        )

        reasons: list[str] = []
        rejected = False

        if assessment.state == HALT:
            adjusted = current.copy()
            rejected = True
            reasons.append("系统风险状态 SYSTEM_HALT：数据/对账/风控服务异常，暂停生成任何可执行调整")
        elif assessment.state == RED:
            # RED 的含义是“总风险敞口不得增加”，而不是冻结全部交易：
            # 允许在现有敞口以内做资产间轮动（卖出高风险的、买入低风险的），但不加杠杆。
            current_exposure = float(current.sum())
            emergency = float(self._get("drawdown_control.emergency_threshold", 0.18))
            drawdown = float(assessment.metrics.get("drawdown", 0.0) or 0.0)
            cap = float(self._get("drawdown_control.reduce_risk_target_exposure", 0.40))
            floor = float(self._get("drawdown_control.reduce_risk_floor_exposure", 0.20))
            # 总敞口上限 = min(max(当前敞口, 防御下限), 降险上限)。
            # 防御下限用于避免“清仓后净值无法修复、RED 永久锁死”的死锁。
            max_allowed = min(max(current_exposure, floor), cap)
            if drawdown >= emergency:
                max_allowed = min(max_allowed,
                                  float(self._get("drawdown_control.emergency_target_exposure", 0.30)))
                reasons.append(f"回撤 {drawdown:.1%} 达到紧急阈值，总敞口压降至 {max_allowed:.1%}")
            if adjusted.sum() > max_allowed + 1e-9:
                adjusted = project_weights(adjusted, max_weight=max_single, sector_map=sector_map,
                                           max_sector=max_sector, max_total=max_allowed)
            reasons.append(
                f"风险状态 RED（当前回撤 {drawdown:.1%}）：总敞口上限 {max_allowed:.1%}，"
                "允许资产间轮动，禁止突破该上限"
            )

        # 单资产裁剪提示
        capped = target[target > max_single + 1e-12]
        for iid in capped.index:
            reasons.append(f"{iid} 目标权重 {capped[iid]:.1%} 超过单资产上限 {max_single:.0%}，已裁剪")
        # 行业裁剪提示
        if sector_map is not None:
            sectors = pd.Series({k: sector_map.get(k, "未分类") for k in target.index})
            exp = target.groupby(sectors).sum()
            for sector, value in exp.items():
                if value > max_sector + 1e-12:
                    reasons.append(f"行业 {sector} 目标暴露 {value:.1%} 超过上限 {max_sector:.0%}，已裁剪")

        # 空仓 / 极小权重清理
        adjusted = adjusted[adjusted >= 0.001]

        # 用“裁剪后的权重”重新评估一次，状态与报告必须反映真正可执行的组合
        final_assessment = self.assess(
            as_of=as_of, nav_history=nav_history, weights=adjusted, sector_map=sector_map,
            quality=quality, target_weights=adjusted, order_notional=estimated_order_notional,
            nav_value=nav_value, portfolio_returns=portfolio_returns,
            correlation_matrix=correlation_matrix,
        )

        approval = None
        if not rejected and final_assessment.state != HALT:
            approval = self._issue_approval(as_of, strategy_id, adjusted, final_assessment.state)
        else:
            rejected = True

        return RiskDecision(
            assessment=final_assessment, approved_weights=adjusted, requested_weights=target,
            rejected=rejected, reasons=reasons, approval=approval,
        )

    # ---------------- 审批签名 ----------------
    def _issue_approval(self, as_of: date, strategy_id: str, weights: pd.Series, state: str) -> RiskApproval:
        digest = _weights_digest(weights)
        nonce = secrets.token_hex(8)
        issued_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        payload = f"{as_of.isoformat()}|{strategy_id}|{digest}|{state}|{issued_at}|{nonce}".encode("utf-8")
        signature = hmac.new(self._secret, payload, hashlib.sha256).hexdigest()
        return RiskApproval(as_of.isoformat(), strategy_id, digest, state, issued_at, nonce, signature, True)

    def verify_approval(self, approval: RiskApproval, as_of: date, strategy_id: str, weights: pd.Series) -> bool:
        if approval is None or not approval.approved:
            return False
        if approval.as_of != as_of.isoformat() or approval.strategy_id != strategy_id:
            return False
        if approval.weights_digest != _weights_digest(weights):
            return False
        payload = (
            f"{approval.as_of}|{approval.strategy_id}|{approval.weights_digest}|"
            f"{approval.state}|{approval.issued_at}|{approval.nonce}"
        ).encode("utf-8")
        expected = hmac.new(self._secret, payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, approval.signature)
