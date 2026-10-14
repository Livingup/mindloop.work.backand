# -*- coding: utf-8 -*-
"""MCP 服务配置 YAML 文件仓储实现。

每个 MCP 服务配置存储为一个独立的 YAML 文件：
  ~/.spread-ai/mcp-servers/{service_id}.yaml
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import yaml

from agent_runtime.mcp.mcp_client.mcp_service_definition import McpServiceDefinition


class YamlMcpServiceRepository:
    """基于 YAML 文件的 MCP 服务配置仓储。

    每个服务配置对应一个 YAML 文件，文件名（不含后缀）即 service_id。
    """

    def __init__(self, config_dir_accessor: Callable[[], str]) -> None:
        """初始化仓储。

        Args:
            config_dir_accessor: 配置目录路径访问器，每次调用时实时从配置中心读取。
        """
        self._config_dir_accessor = config_dir_accessor
        self._config_dir.mkdir(parents=True, exist_ok=True)

    @property
    def _config_dir(self) -> Path:
        """实时从配置中心读取 MCP 服务配置目录路径（避免配置数据冗余）。"""
        return Path(self._config_dir_accessor())

    def find_all(self) -> list[McpServiceDefinition]:
        """列出所有服务配置。

        扫描配置目录下所有 .yaml 文件，解析并返回。
        跳过解析失败的文件（记录警告但不中断）。
        """
        definitions: list[McpServiceDefinition] = []
        if not self._config_dir.exists():
            return definitions

        for yaml_file in sorted(self._config_dir.glob("*.yaml")):
            service_id = yaml_file.stem
            definition = self._load_file(service_id, yaml_file)
            if definition is not None:
                definitions.append(definition)

        return definitions

    def find_by_id(self, service_id: str) -> McpServiceDefinition | None:
        """按 ID 查找服务配置。"""
        yaml_file = self._get_file_path(service_id)
        if not yaml_file.exists():
            return None
        return self._load_file(service_id, yaml_file)

    def save(self, definition: McpServiceDefinition) -> None:
        """保存服务配置（新增或更新）。

        将 McpServiceDefinition 序列化为 YAML 并写入文件。
        """
        yaml_file = self._get_file_path(definition.service_id)
        data = definition.to_dict()
        with open(yaml_file, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)

    def delete(self, service_id: str) -> bool:
        """删除服务配置文件。"""
        yaml_file = self._get_file_path(service_id)
        if yaml_file.exists():
            yaml_file.unlink()
            return True
        return False

    def contains(self, service_id: str) -> bool:
        """是否存在指定服务配置。"""
        return self._get_file_path(service_id).exists()

    def config_dir_mtime(self) -> float:
        """获取配置目录的最大修改时间戳。

        返回配置目录自身及其下所有 YAML 文件中最大的 mtime。
        用于跨进程变更检测。
        """
        config_dir = self._config_dir
        if not config_dir.exists():
            return 0.0
        max_mtime = config_dir.stat().st_mtime
        for yaml_file in config_dir.glob("*.yaml"):
            file_mtime = yaml_file.stat().st_mtime
            if file_mtime > max_mtime:
                max_mtime = file_mtime
        return max_mtime

    # ── 内部方法 ──

    def _get_file_path(self, service_id: str) -> Path:
        """获取服务配置文件路径。"""
        return self._config_dir / f"{service_id}.yaml"

    def _load_file(
        self, service_id: str, yaml_file: Path
    ) -> McpServiceDefinition | None:
        """从 YAML 文件加载服务配置。

        解析失败时返回 None。
        """
        try:
            with open(yaml_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if not isinstance(data, dict):
                return None
            return McpServiceDefinition.from_dict(service_id, data)
        except Exception:
            return None
