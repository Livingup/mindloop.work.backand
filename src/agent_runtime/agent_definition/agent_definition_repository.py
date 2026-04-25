# -*- coding: utf-8 -*-
"""Agent 定义仓储接口（领域层）。

领域服务只依赖此接口，不关心 Agent 定义最终存储在 YAML、数据库还是其他介质中。
"""
from __future__ import annotations

from typing import Protocol

from agent_runtime.agent_definition.agent_definition import AgentDefinition


class AgentDefinitionRepository(Protocol):
    """Agent 定义仓储接口。

    仓储以集合语义管理 AgentDefinition 聚合根，agent_id 是唯一业务标识。
    """

    def find_all(self) -> list[AgentDefinition]:
        """返回所有已持久化的 Agent 定义。"""
        ...

    def find_by_id(self, agent_id: str) -> AgentDefinition | None:
        """按 agent_id 查找 Agent 定义。未找到时返回 None。"""
        ...

    def save(self, definition: AgentDefinition) -> None:
        """保存 Agent 定义。agent_id 已存在时覆写，不存在时新增。"""
        ...

    def delete(self, agent_id: str) -> bool:
        """删除 Agent 定义。返回是否实际删除了文件。"""
        ...

    def contains(self, agent_id: str) -> bool:
        """判断指定 agent_id 的 Agent 定义是否存在。"""
        ...
