# -*- coding: utf-8 -*-
"""Agent 定义 YAML 文件仓储实现。

每个 Agent 定义存储为一个独立 YAML 文件：
  ~/.spread-ai/agents/{agent_id}.yaml
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import yaml

from agent_runtime.agent_definition.agent_definition import AgentDefinition


class YamlAgentDefinitionRepository:
    """基于 YAML 文件的 Agent 定义仓储。

    文件名主名即 agent_id，文件内容保存 AgentDefinition 的完整静态模板数据。
    仓储不缓存配置目录；每次访问目录都通过 accessor 从 ConfigRegistry 实时读取，支持配置热更新。
    """

    def __init__(self, definitions_dir_accessor: Callable[[], str]) -> None:
        """初始化仓储并确保定义目录存在。

        Args:
            definitions_dir_accessor: Agent 定义目录访问器，每次调用时实时读取配置中心数据。
        """
        # Agent 定义目录访问器。仓储不保存配置值副本，避免 ConfigRegistry 热更新后读到旧目录。
        self._definitions_dir_accessor = definitions_dir_accessor
        self._definitions_dir.mkdir(parents=True, exist_ok=True)

    @property
    def _definitions_dir(self) -> Path:
        """实时读取 Agent 定义 YAML 文件目录路径。"""
        return Path(self._definitions_dir_accessor())

    def find_all(self) -> list[AgentDefinition]:
        """列出所有 Agent 定义。

        扫描定义目录下所有 .yaml 文件，逐个解析为 AgentDefinition。
        单个文件解析失败时跳过，避免一个损坏定义阻断管理后台查看其它定义。
        """
        definitions: list[AgentDefinition] = []
        if not self._definitions_dir.exists():
            return definitions

        for yaml_file in sorted(self._definitions_dir.glob("*.yaml")):
            definition = self._load_file(yaml_file.stem, yaml_file)
            if definition is not None:
                definitions.append(definition)
        return definitions

    def find_by_id(self, agent_id: str) -> AgentDefinition | None:
        """按 agent_id 查找 Agent 定义。"""
        yaml_file = self._get_file_path(agent_id)
        if not yaml_file.exists():
            return None
        return self._load_file(agent_id, yaml_file)

    def save(self, definition: AgentDefinition) -> None:
        """保存 Agent 定义到 YAML 文件。"""
        self._definitions_dir.mkdir(parents=True, exist_ok=True)
        yaml_file = self._get_file_path(definition.agent_id)
        with open(yaml_file, "w", encoding="utf-8") as f:
            yaml.dump(
                definition.to_dict(),
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )

    def delete(self, agent_id: str) -> bool:
        """删除 Agent 定义文件。"""
        yaml_file = self._get_file_path(agent_id)
        if yaml_file.exists():
            yaml_file.unlink()
            return True
        return False

    def contains(self, agent_id: str) -> bool:
        """判断 Agent 定义文件是否存在。"""
        return self._get_file_path(agent_id).exists()

    def _get_file_path(self, agent_id: str) -> Path:
        """获取 Agent 定义文件路径。"""
        return self._definitions_dir / f"{agent_id}.yaml"

    def _load_file(self, agent_id: str, yaml_file: Path) -> AgentDefinition | None:
        """从 YAML 文件加载 Agent 定义。

        解析失败时返回 None；调用方按场景决定是否把 None 转换为未找到或跳过。
        """
        try:
            with open(yaml_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if not isinstance(data, dict):
                return None
            return AgentDefinition.from_dict(agent_id, data)
        except Exception:
            return None
