"""读取项目已确认的最小数据合同，不包含业务流程实现。

信封字段保持向前兼容：修订版样例在信封之外追加 ``entities`` 等键，
旧读取方通过 ``DomainRecord`` 仍只取信封字段，不因未知键报错。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class DomainRecord:
    schema_version: int
    record_id: str
    domain: str
    occurred_at: str
    revision: int
    source: str


_ENVELOPE_FIELDS = {f.name for f in fields(DomainRecord)}


def load_record(path: Path) -> DomainRecord:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return DomainRecord(**{k: payload[k] for k in _ENVELOPE_FIELDS})
