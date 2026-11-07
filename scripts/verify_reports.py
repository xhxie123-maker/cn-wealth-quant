"""报告验收：文件齐全、指标可解析、决策卡字段完整、审计链未被篡改。

用法：python scripts/verify_reports.py reports/demo
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

REQUIRED = [
    "metrics.json", "nav.csv", "trades.csv", "rejected_orders.csv",
    "target_history.json", "decision_cards.csv", "decision_batch.json",
    "risk_snapshot.json", "stress_tests.json", "quality_report.json",
    "decision_report.md", "run_manifest.json", "audit_log.jsonl",
]

CARD_FIELDS = ["资产代码", "当前信号", "当前仓位", "目标仓位", "建议金额(元)", "风险等级", "决策理由", "失效条件"]


def main(argv: list[str]) -> int:
    report = Path(argv[1]) if len(argv) > 1 else ROOT / "reports" / "demo"
    report = report if report.is_absolute() else (ROOT / report)
    problems: list[str] = []

    print(f"验收目录：{report}")
    if not report.is_dir():
        print("错误：目录不存在，请先运行 demo")
        return 2

    for name in REQUIRED:
        path = report / name
        if not path.exists():
            problems.append(f"缺少文件：{name}")
        else:
            print(f"  ok  {name}  ({path.stat().st_size:,} bytes)")

    metrics_path = report / "metrics.json"
    if metrics_path.exists():
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        required_metrics = ["cagr", "annualized_volatility", "sharpe", "max_drawdown", "final_nav", "trades"]
        for key in required_metrics:
            if key not in metrics:
                problems.append(f"metrics.json 缺少指标：{key}")
        print(f"  指标：CAGR={metrics.get('cagr'):.4f}  MDD={metrics.get('max_drawdown'):.4f}  "
              f"Sharpe={metrics.get('sharpe'):.3f}  trades={metrics.get('trades')}")

    cards_path = report / "decision_cards.csv"
    if cards_path.exists():
        import pandas as pd

        cards = pd.read_csv(cards_path)
        missing = [field for field in CARD_FIELDS if field not in cards.columns]
        if missing:
            problems.append(f"决策卡缺少字段：{missing}")
        else:
            print(f"  决策卡：{len(cards)} 张，十字段关键字段完整")

    audit_path = report / "audit_log.jsonl"
    if audit_path.exists():
        from wealth_quant.audit.log import AuditLog

        ok = AuditLog(audit_path).verify()
        print(f"  审计链 verify() = {ok}")
        if not ok:
            problems.append("审计链校验失败（文件可能被篡改）")

    if problems:
        print("\n验收失败：")
        for item in problems:
            print(f"  - {item}")
        return 1
    print("\n验收通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
