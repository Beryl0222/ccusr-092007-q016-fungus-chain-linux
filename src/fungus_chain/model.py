"""事件链领域模型。

在信封合同（:mod:`fungus_chain.contracts`）之上，把一次误食联检涉及的
各类实体加载为 :class:`Case`。实体之间一律以 ``*_ref`` 标识关联，
模型本身不做业务裁决；裁决见 :mod:`fungus_chain.validation`。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Entity:
    """一条带类型与稳定标识的事件链记录。"""

    id: str
    type: str
    data: dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


@dataclass(frozen=True)
class Case:
    """一次暴露事件的完整链条（信封字段 + 实体集合）。"""

    schema_version: int
    record_id: str
    domain: str
    occurred_at: str
    revision: int
    source: str
    entities: tuple[Entity, ...]

    def by_type(self, type_name: str) -> list[Entity]:
        return [e for e in self.entities if e.type == type_name]

    def get(self, entity_id: str) -> Entity:
        for entity in self.entities:
            if entity.id == entity_id:
                return entity
        raise KeyError(entity_id)

    def find(self, entity_id: str) -> Entity | None:
        for entity in self.entities:
            if entity.id == entity_id:
                return entity
        return None


def load_case(path: str | Path) -> Case:
    """从样例文件加载事件链；缺少 ``entities`` 时按空链处理。"""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    entities = tuple(
        Entity(id=item["id"], type=item["type"], data=item)
        for item in payload.get("entities", [])
    )
    return Case(
        schema_version=payload["schema_version"],
        record_id=payload["record_id"],
        domain=payload["domain"],
        occurred_at=payload["occurred_at"],
        revision=payload["revision"],
        source=payload["source"],
        entities=entities,
    )
