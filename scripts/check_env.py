"""环境自检：核心依赖、Python 版本、可选依赖。

用法：python scripts/check_env.py
缺核心依赖时返回非零退出码；可选依赖缺失只提示，不阻断。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

CORE = ["numpy", "pandas", "yaml"]
OPTIONAL = ["pytest", "streamlit", "duckdb", "sqlalchemy"]


def main() -> int:
    print(f"Python: {sys.version.split()[0]} ({sys.executable})")
    if sys.version_info < (3, 11):
        print("错误：需要 Python >= 3.11")
        return 1

    missing = []
    for name in CORE:
        try:
            module = importlib.import_module(name)
            version = getattr(module, "__version__", "?")
            print(f"[核心] {name:10s} {version}")
        except ImportError:
            print(f"[核心] {name:10s} 缺失")
            missing.append(name)

    for name in OPTIONAL:
        try:
            module = importlib.import_module(name)
            print(f"[可选] {name:10s} {getattr(module, '__version__', '?')}")
        except ImportError:
            print(f"[可选] {name:10s} 未安装（不影响核心回测）")

    root = Path(__file__).resolve().parents[1]
    sample = root / "data" / "sample" / "bars.csv"
    print(f"示例数据：{'存在' if sample.exists() else '未生成（运行 generate-sample）'} -> {sample}")

    if missing:
        print("请先安装核心依赖：pip install -e .")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
