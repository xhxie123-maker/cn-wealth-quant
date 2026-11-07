"""审计与版本：追加式哈希链日志 + 策略版本快照。

要求（建设手册模块八 / 验收标准）：每一条决策都能追踪到数据与模型来源；
参数修改必须留痕，禁止覆盖旧版本回测结果。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))


class AuditLog:
    """追加式 JSONL 哈希链；任何一行被篡改都会导致链校验失败。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _last_hash(self) -> str:
        if not self.path.exists():
            return "0" * 64
        last = None
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    last = line
        if last is None:
            return "0" * 64
        try:
            return json.loads(last)["hash"]
        except Exception:  # 损坏的日志不静默忽略
            raise RuntimeError(f"审计日志末行无法解析: {self.path}")

    def append(self, event_type: str, payload: dict[str, Any], actor: str = "system") -> dict[str, Any]:
        prev_hash = self._last_hash()
        record = {
            "seq": self._next_seq(),
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "event_type": event_type,
            "actor": actor,
            "payload": payload,
            "prev_hash": prev_hash,
        }
        record["hash"] = hashlib.sha256((_canonical(record) + prev_hash).encode("utf-8")).hexdigest()
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return record

    def _next_seq(self) -> int:
        if not self.path.exists():
            return 1
        count = 0
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    count += 1
        return count + 1

    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    out.append(json.loads(line))
        return out

    def verify(self) -> bool:
        prev = "0" * 64
        for record in self.records():
            body = {k: v for k, v in record.items() if k != "hash"}
            expected = hashlib.sha256((_canonical(body) + prev).encode("utf-8")).hexdigest()
            if record.get("prev_hash") != prev or record.get("hash") != expected:
                return False
            prev = record["hash"]
        return True


class StrategyRegistrySnapshot:
    """把策略配置固化成带版本与参数哈希的快照，作为回测/决策的输入凭证。"""

    def __init__(self, descriptions: Iterable[dict[str, Any]]):
        self.descriptions = list(descriptions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "strategies": self.descriptions,
            "digest": hashlib.sha256(_canonical(self.descriptions).encode("utf-8")).hexdigest()[:16],
        }
