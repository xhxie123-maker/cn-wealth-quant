"""Streamlit 决策台（只读）。

运行：
    streamlit run src/wealth_quant/ui/app.py
或：
    python -m wealth_quant.cli serve

设计原则：
- 只读 reports/ 下的产物，不在界面里改参数、不触发交易；
- 没有产物时明确提示先运行 demo，而不是展示空白图表；
- 所有页面常驻免责声明。
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
REPORTS = ROOT / "reports"

DISCLAIMER = (
    "本页面由量化模型自动生成，仅用于辅助决策，不构成投资建议；"
    "所有交易需人工复核后由投资者自行决定，系统不具备下单权限。"
)


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def _pct(value, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}%}"
    except (TypeError, ValueError):
        return "—"


def _num(value, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _regime_from_benchmark(nav: pd.DataFrame) -> tuple[str, pd.DataFrame]:
    """用宽基指数均线/波动做一个可解释的状态判定（与后端口径一致）。"""
    if nav.empty or "benchmark" not in nav.columns:
        return "—", pd.DataFrame()
    frame = nav.dropna(subset=["benchmark"]).copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame = frame.sort_values("trade_date").set_index("trade_date")
    frame["ma200"] = frame["benchmark"].rolling(200, min_periods=120).mean()
    frame["ret"] = frame["benchmark"].pct_change()
    frame["vol60"] = frame["ret"].rolling(60, min_periods=40).std() * (252 ** 0.5)
    frame["vol120"] = frame["ret"].rolling(120, min_periods=80).std() * (252 ** 0.5)
    latest = frame.iloc[-1]
    trend = latest["benchmark"] / latest["ma200"] - 1 if pd.notna(latest["ma200"]) else 0.0
    vol_ratio = latest["vol60"] / latest["vol120"] if latest["vol120"] else 1.0
    if trend > 0.03 and vol_ratio < 1.2:
        regime = "A（趋势上行 / 低波动）"
    elif trend > -0.03:
        regime = "B（震荡 / 中性）"
    elif vol_ratio >= 1.2:
        regime = "D（下跌 / 高波动）"
    else:
        regime = "C（趋势下行 / 低波动）"
    return regime, frame


def _render_overview(st, report: Path, metrics: dict, risk: dict, cards: pd.DataFrame) -> None:
    st.subheader("总览")
    state = risk.get("state", "—")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("年化收益", _pct(metrics.get("cagr")))
    c2.metric("年化波动", _pct(metrics.get("annualized_volatility")))
    c3.metric("最大回撤", _pct(metrics.get("max_drawdown")))
    c4.metric("风险状态", state)

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Sharpe", _num(metrics.get("sharpe")))
    c6.metric("年化换手", _pct(metrics.get("annualized_turnover")))
    c7.metric("成交笔数", metrics.get("trades", "—"))
    c8.metric("期末净值", f"{metrics.get('final_nav', 0):,.0f}")

    nav = _read_csv(report / "nav.csv")
    if not nav.empty:
        nav["trade_date"] = pd.to_datetime(nav["trade_date"])
        nav = nav.set_index("trade_date")
        st.line_chart(nav[["nav", "benchmark"]].rename(columns={"nav": "组合净值", "benchmark": "沪深300"}))
    st.caption(f"数据区间：{metrics.get('start')} 至 {metrics.get('end')}；决策卡 {len(cards)} 张。")
    st.info(DISCLAIMER)


def _render_regime(st, report: Path, nav: pd.DataFrame) -> None:
    st.subheader("市场状态")
    regime, frame = _regime_from_benchmark(nav)
    st.markdown(f"**当前状态：{regime}**")
    if not frame.empty:
        latest = frame.iloc[-1]
        c1, c2, c3 = st.columns(3)
        c1.metric("沪深300 / MA200", _pct(latest["benchmark"] / latest["ma200"] - 1 if pd.notna(latest["ma200"]) else None))
        c2.metric("60日年化波动", _pct(latest["vol60"]))
        c3.metric("120日年化波动", _pct(latest["vol120"]))
        st.line_chart(frame[["benchmark", "ma200"]])
        st.caption("均线与波动只用于解释状态，不构成收益预测。")

    macro_path = ROOT / "data" / "sample" / "macro.csv"
    macro = _read_csv(macro_path)
    if not macro.empty:
        macro["period"] = pd.to_datetime(macro["period"])
        pivot = macro.pivot_table(index="period", columns="indicator", values="value", aggfunc="last").tail(12)
        st.markdown("**近 12 个月宏观指标**")
        st.dataframe(pivot, width="stretch")
    st.info(DISCLAIMER)


def _render_candidates(st, report: Path, cards: pd.DataFrame) -> None:
    st.subheader("机会筛选")
    targets = _read_json(report / "target_history.json")
    if isinstance(targets, list) and targets:
        latest = targets[-1]
        rows = [
            {"标的": k, "策略目标权重": v}
            for k, v in latest.get("target_weights", {}).items()
        ]
        target_df = pd.DataFrame(rows).sort_values("策略目标权重", ascending=False)
        st.markdown(f"**最近一期策略目标（{latest.get('date')}）**")
        st.dataframe(target_df, width="stretch", hide_index=True)
        st.markdown("**风控裁剪后权重**")
        approved = pd.DataFrame(
            [{"标的": k, "风控后权重": v} for k, v in latest.get("approved_weights", {}).items()]
        ).sort_values("风控后权重", ascending=False)
        st.dataframe(approved, width="stretch", hide_index=True)
    else:
        st.caption("暂无 target_history.json，请先运行 demo。")
    st.info(DISCLAIMER)


def _render_portfolio(st, cards: pd.DataFrame) -> None:
    st.subheader("组合分析")
    if cards.empty:
        st.caption("暂无决策卡数据。")
        st.info(DISCLAIMER)
        return
    weights = cards[["name", "current_weight", "target_weight"]].copy()
    weights["current_weight"] = pd.to_numeric(weights["current_weight"], errors="coerce").fillna(0)
    weights["target_weight"] = pd.to_numeric(weights["target_weight"], errors="coerce").fillna(0)
    weights = weights.set_index("name")
    st.markdown("**当前仓位 vs 目标仓位**")
    st.bar_chart(weights)

    exposure = float(weights["current_weight"].sum())
    hhi = float((weights["current_weight"] ** 2).sum())
    c1, c2, c3 = st.columns(3)
    c1.metric("当前总敞口", _pct(exposure))
    c2.metric("HHI 集中度", _num(hhi, 3))
    c3.metric("现金比例", _pct(max(0.0, 1 - exposure)))
    st.caption("HHI 越低越分散；单资产上限 20%，行业上限 35%。")
    st.info(DISCLAIMER)


def _render_decisions(st, cards: pd.DataFrame) -> None:
    st.subheader("决策建议")
    if cards.empty:
        st.caption("暂无决策卡，请先运行 demo。")
        st.info(DISCLAIMER)
        return
    display = cards.rename(columns={
        "name": "资产", "signal": "信号", "current_weight": "当前仓位",
        "target_weight": "目标仓位", "suggested_amount": "建议金额",
        "risk_level": "风险等级", "horizon": "持有期限",
    })
    columns = ["资产", "信号", "当前仓位", "目标仓位", "建议金额", "风险等级", "持有期限"]
    st.dataframe(
        display[[c for c in columns if c in display.columns]],
        width="stretch", hide_index=True,
    )
    for _, row in cards.iterrows():
        with st.expander(f"{row.get('name')}（{row.get('instrument_id')}） · {row.get('signal')}"):
            st.markdown(f"**决策理由**：{row.get('rationale', '—')}")
            st.markdown(f"**失效条件**：{row.get('invalidation', '—')}")
            st.markdown(f"**数据时点**：{row.get('as_of', '—')}｜**策略**：{row.get('strategy', '—')}")
    st.download_button(
        "下载决策卡 CSV",
        data=cards.to_csv(index=False).encode("utf-8-sig"),
        file_name="decision_cards.csv",
        mime="text/csv",
    )
    st.info(DISCLAIMER)


def _render_risk(st, report: Path, metrics: dict, risk: dict) -> None:
    st.subheader("回测与风险")
    pairs = [
        ("年化收益", metrics.get("cagr")), ("年化波动", metrics.get("annualized_volatility")),
        ("Sharpe", metrics.get("sharpe")), ("Sortino", metrics.get("sortino")),
        ("最大回撤", metrics.get("max_drawdown")), ("Calmar", metrics.get("calmar")),
        ("VaR 95%", metrics.get("var_95")), ("ES 95%", metrics.get("es_95")),
        ("Beta", metrics.get("beta")), ("Alpha", metrics.get("alpha")),
        ("信息比率", metrics.get("information_ratio")), ("胜率", metrics.get("win_rate")),
    ]
    table = pd.DataFrame(
        [{"指标": k, "数值": (_pct(v) if k not in {"Sharpe", "Sortino", "Calmar", "Beta", "信息比率"} else _num(v))}
         for k, v in pairs]
    )
    st.dataframe(table, width="stretch", hide_index=True)

    stress = _read_json(report / "stress_tests.json")
    if isinstance(stress, dict):
        stress = stress.get("scenarios", [])
    if isinstance(stress, list) and stress:
        st.markdown("**压力测试**")
        st.dataframe(pd.DataFrame([
            {"情景": s.get("name"), "组合冲击": _pct(s.get("portfolio_impact")), "说明": s.get("description")}
            for s in stress
        ]), width="stretch", hide_index=True)

    checks = risk.get("checks", [])
    if checks:
        st.markdown("**风控逐项检查**")
        st.dataframe(pd.DataFrame([
            {"检查": c.get("name"), "等级": c.get("level"), "通过": c.get("passed"),
             "数值": c.get("value"), "阈值": c.get("limit"), "说明": c.get("message")}
            for c in checks
        ]), width="stretch", hide_index=True)

    rejected = _read_csv(report / "rejected_orders.csv")
    if not rejected.empty:
        st.markdown("**被拒订单**")
        st.dataframe(rejected.head(50), width="stretch", hide_index=True)
    st.info(DISCLAIMER)


def main() -> None:
    import streamlit as st

    st.set_page_config(page_title="稳健理财量化决策台", layout="wide")
    st.title("稳健理财量化决策台")
    st.caption("中国市场 · 股票 + 场内 ETF · 中期持有 · 人工确认 · 不自动下单")

    report_dirs = sorted([p for p in REPORTS.glob("*") if p.is_dir()]) if REPORTS.exists() else []
    if not report_dirs:
        st.warning("还没有任何运行产物。请先在项目根目录运行：")
        st.code("python -m wealth_quant.cli generate-sample\npython -m wealth_quant.cli demo")
        return

    names = [p.name for p in report_dirs]
    default = names.index("demo") if "demo" in names else 0
    selected = st.sidebar.selectbox("报告目录", names, index=default)
    report = REPORTS / selected
    page = st.sidebar.radio(
        "页面",
        ["总览", "市场状态", "机会筛选", "组合分析", "决策建议", "回测与风险"],
    )
    st.sidebar.markdown("---")
    st.sidebar.caption("只读界面：不改参数、不触发交易。")

    metrics = _read_json(report / "metrics.json")
    risk = _read_json(report / "risk_snapshot.json")
    cards = _read_csv(report / "decision_cards.csv")
    nav = _read_csv(report / "nav.csv")

    if not metrics:
        st.warning(f"{report} 下没有 metrics.json，请先运行 demo 或 backtest。")
        return

    if page == "总览":
        _render_overview(st, report, metrics, risk, cards)
    elif page == "市场状态":
        _render_regime(st, report, nav)
    elif page == "机会筛选":
        _render_candidates(st, report, cards)
    elif page == "组合分析":
        _render_portfolio(st, cards)
    elif page == "决策建议":
        _render_decisions(st, cards)
    else:
        _render_risk(st, report, metrics, risk)

    st.sidebar.markdown("---")
    st.sidebar.caption("仅辅助决策，不构成投资建议。")


if __name__ == "__main__":
    main()
