"""pytest 公共夹具：把 src 加入导入路径，加载项目配置与小型合成数据。"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wealth_quant.config import ProjectConfig  # noqa: E402
from wealth_quant.data.providers import generate_synthetic_dataset  # noqa: E402


@pytest.fixture(scope="session")
def project_root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def cfg() -> ProjectConfig:
    return ProjectConfig.load(ROOT)


@pytest.fixture(scope="session")
def small_dataset(cfg: ProjectConfig):
    """6 个标的、3 年数据；比完整示例小，保证测试速度。"""
    specs = cfg.universe["instruments"][:6]
    return generate_synthetic_dataset(specs, date(2022, 1, 1), date(2024, 12, 31), seed=7)
