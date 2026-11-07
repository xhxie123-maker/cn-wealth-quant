"""报告输出：JSON / CSV / Markdown，便于归档、审计与界面读取。"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


def json_default(obj: Any) -> Any:
    if isinstance(obj, (date, datetime, pd.Timestamp)):
        return obj.isoformat()
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        value = float(obj)
        return None if not np.isfinite(value) else value
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, pd.Series):
        return obj.to_dict()
    if isinstance(obj, pd.DataFrame):
        return obj.to_dict(orient="records")
    if is_dataclass(obj):
        return asdict(obj)
    return str(obj)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, default=json_default)


def write_frame(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8-sig")


def _fmt_pct(value: Any) -> str:
    try:
        if value is None or (isinstance(value, float) and not np.isfinite(value)):
            return "—"
        return f"{float(value):.2%}"
    except (TypeError, ValueError):
        return "—"


def _fmt_num(value: Any, digits: int = 2) -> str:
    try:
        if value is None or (isinstance(value, float) and not np.isfinite(value)):
            return "—"
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def decision_markdown(batch: Any, metrics: dict[str, Any], quality_summary: dict[str, Any],
                      stress: list[Any] | None = None) -> str:
    lines: list[str] = []
    lines.append(f"# 稳健理财量化决策报告 · {batch.as_of}")
    lines.append("")
    lines.append("> 本报告由量化模型自动生成，仅用于辅助决策，不构成投资建议；所有交易需人工确认。")
    lines.append("")
    lines.append("## 一、本期结论")
    lines.append("")
    lines.append(f"- 策略：**{batch.strategy_name} {batch.strategy_version}**")
    lines.append(f"- 风险状态：**{batch.risk_state}**（{'已通过风控审批' if batch.approval_verified else '未通过风控审批，仅展示观望建议'}）")
    lines.append(f"- 数据质量：{'通过' if quality_summary.get('passed') else '存在错误'}"
                 f"（错误 {quality_summary.get('errors', 0)} 项，警告 {quality_summary.get('warnings', 0)} 项）")
    if metrics:
        lines.append(f"- 回测区间：{metrics.get('start')} 至 {metrics.get('end')}，"
                     f"年化 {_fmt_pct(metrics.get('cagr'))}，最大回撤 {_fmt_pct(metrics.get('max_drawdown'))}，"
                     f"Sharpe {_fmt_num(metrics.get('sharpe'))}")
    lines.append("")

    if batch.risk_reasons:
        lines.append("### 风控意见")
        lines.append("")
        for reason in batch.risk_reasons:
            lines.append(f"- {reason}")
        lines.append("")

    lines.append("## 二、投资建议明细")
    lines.append("")
    lines.append("| 资产 | 信号 | 当前仓位 | 目标仓位 | 建议金额 | 风险等级 | 决策理由 | 失效条件 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for card in batch.cards:
        rationale = "<br>".join(card.rationale[:3])
        invalidation = "<br>".join(card.invalidation[:2])
        lines.append(
            f"| {card.name}（{card.instrument_id}） | {card.signal} | {card.current_weight:.1%} | "
            f"{card.target_weight:.1%} | {card.suggested_amount:,.0f} | {card.risk_level} | "
            f"{rationale} | {invalidation} |"
        )
    lines.append("")

    if stress:
        lines.append("## 三、压力测试")
        lines.append("")
        lines.append("| 情景 | 组合冲击 | 说明 |")
        lines.append("|---|---|---|")
        for item in stress:
            impact = item.portfolio_impact if hasattr(item, "portfolio_impact") else item.get("portfolio_impact")
            name = item.name if hasattr(item, "name") else item.get("name")
            desc = item.description if hasattr(item, "description") else item.get("description")
            lines.append(f"| {name} | {_fmt_pct(impact)} | {desc} |")
        lines.append("")

    lines.append("## 四、绩效与风险指标")
    lines.append("")
    if metrics:
        pairs = [
            ("年化收益 CAGR", _fmt_pct(metrics.get("cagr"))),
            ("年化波动率", _fmt_pct(metrics.get("annualized_volatility"))),
            ("Sharpe", _fmt_num(metrics.get("sharpe"))),
            ("Sortino", _fmt_num(metrics.get("sortino"))),
            ("最大回撤", _fmt_pct(metrics.get("max_drawdown"))),
            ("Calmar", _fmt_num(metrics.get("calmar"))),
            ("1日 95% VaR", _fmt_pct(metrics.get("var_95"))),
            ("1日 95% ES", _fmt_pct(metrics.get("es_95"))),
            ("年化换手", _fmt_pct(metrics.get("annualized_turnover"))),
            ("胜率", _fmt_pct(metrics.get("win_rate"))),
            ("Alpha", _fmt_pct(metrics.get("alpha"))),
            ("Beta", _fmt_num(metrics.get("beta"))),
        ]
        lines.append("| 指标 | 数值 |")
        lines.append("|---|---|")
        for k, v in pairs:
            lines.append(f"| {k} | {v} |")
        lines.append("")

    lines.append("## 五、必须遵守的边界")
    lines.append("")
    for d in batch.disclaimers:
        lines.append(f"- {d}")
    lines.append("")
    lines.append(f"生成时间：{batch.generated_at}　|　人工确认：{'必须' if batch.manual_confirmation_required else '否'}"
                 f"　|　自动执行：{'是' if batch.auto_execution else '否'}")
    lines.append("")
    return "\n".join(lines)
