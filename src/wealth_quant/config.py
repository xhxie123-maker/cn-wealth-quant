"""配置加载：system.yaml / risk_policy.yaml / strategies.yaml / universe.yaml。

所有路径相对于项目根目录解析；可通过环境变量 CNWQ_ROOT 指定根目录。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


def project_root() -> Path:
    env = os.environ.get("CNWQ_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    # src/wealth_quant/config.py -> 项目根
    return Path(__file__).resolve().parents[2]


def deep_get(data: dict[str, Any], dotted: str, default: Any = None) -> Any:
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"配置文件顶层必须是映射: {path}")
    return data


@dataclass
class ProjectConfig:
    root: Path
    system: dict[str, Any]
    risk_policy: dict[str, Any]
    strategies: dict[str, Any]
    universe: dict[str, Any]

    @classmethod
    def load(cls, root: str | Path | None = None) -> "ProjectConfig":
        root_path = Path(root).resolve() if root else project_root()
        system = load_yaml(root_path / "config" / "system.yaml")
        policy_file = deep_get(system, "risk.policy_file", "config/risk_policy.yaml")
        strategy_file = deep_get(system, "strategy.config_file", "config/strategies.yaml")
        return cls(
            root=root_path,
            system=system,
            risk_policy=load_yaml(root_path / policy_file),
            strategies=load_yaml(root_path / strategy_file),
            universe=load_yaml(root_path / "config" / "universe.yaml"),
        )

    def get(self, dotted: str, default: Any = None) -> Any:
        return deep_get(self.system, dotted, default)

    def resolve(self, relative: str | Path) -> Path:
        p = Path(relative)
        return p if p.is_absolute() else self.root / p

    @property
    def base_currency(self) -> str:
        return self.get("project.base_currency", "CNY")
