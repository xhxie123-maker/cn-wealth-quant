"""统一日志。

日志只记录运行事实（时间、模块、事件、对象 ID），不记录任何密钥或账户敏感信息。
"""

from __future__ import annotations

import logging
import sys

_CONFIGURED = False


def get_logger(name: str = "wealth_quant") -> logging.Logger:
    global _CONFIGURED
    if not _CONFIGURED:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", "%H:%M:%S")
        )
        root = logging.getLogger("wealth_quant")
        root.setLevel(logging.INFO)
        root.addHandler(handler)
        root.propagate = False
        _CONFIGURED = True
    return logging.getLogger(name)
