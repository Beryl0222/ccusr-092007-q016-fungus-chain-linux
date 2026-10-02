"""读取项目已确认的最小数据合同，不包含业务流程实现。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class DomainRecord:
    schema_version: int
    record_id: str
    domain: str
    occurred_at: str
    revision: int
    source: str

def load_record(path: Path) -> DomainRecord:
    """读取信封字段。v2 样例在信封之外带有完整样本链条束，多余字段被忽略。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    envelope = {key: payload[key] for key in DomainRecord.__dataclass_fields__ if key in payload}
    return DomainRecord(**envelope)
