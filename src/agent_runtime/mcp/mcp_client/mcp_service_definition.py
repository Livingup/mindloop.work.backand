# -*- coding: utf-8 -*-
"""MCP 服务配置定义 — 领域实体（聚合根）。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from agent_runtime.mcp.mcp_client.mcp_transport_config import (
    StdioTransportConfig,
    StreamableHttpTransportConfig,
    TransportConfig,
)


# service_id 允许的格式：小写字母、数字、连字符、下划线，3~64 字符
_SERVICE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9\-_]{1,62}[a-z0-9]$")


@dataclass
class McpServiceDefinition:
    """MCP 服务配置定义。

    描述一个外部 MCP 服务的连接方式和元数据。
    作为聚合根，自身携带业务规则校验。
    """

    service_id: str
    """服务唯一标识，同时也是 YAML 配置文件名（不含后缀）。"""

    name: str
    """人类可读的服务名称。"""

    transport: TransportConfig
    """传输配置（StdioTransportConfig 或 StreamableHttpTransportConfig）。"""

    description: str | None = None
    """服务描述。"""

    enabled: bool = True
    """是否启用此服务。"""

    usage_guide: str | None = None
    """本地补充的使用指南。与 MCP 服务自带的 instructions 合并后返回给 Agent。"""

    # ── 便捷属性 ──

    @property
    def transport_type(self) -> str:
        """传输方式标识：'stdio' 或 'streamable-http'。"""
        if isinstance(self.transport, StdioTransportConfig):
            return "stdio"
        return "streamable-http"

    # ── 业务规则 ──

    def validate(self) -> None:
        """校验配置完整性。

        校验规则：
        - service_id 必须符合文件名规范（小写字母、数字、连字符/下划线，3~64 字符）
        - name 不能为空
        - transport 必须是 StdioTransportConfig 或 StreamableHttpTransportConfig

        Raises:
            ValueError: 校验失败时抛出，包含具体错误描述。
        """
        if not _SERVICE_ID_PATTERN.match(self.service_id):
            raise ValueError(
                f"service_id '{self.service_id}' 格式无效，"
                "要求：小写字母/数字/连字符/下划线，3~64 字符，不能以连字符开头或结尾"
            )

        if not self.name or not self.name.strip():
            raise ValueError("name 不能为空")

        if not isinstance(self.transport, (StdioTransportConfig, StreamableHttpTransportConfig)):
            raise ValueError(
                f"transport 类型 '{type(self.transport).__name__}' 无效，"
                "支持的类型：StdioTransportConfig, StreamableHttpTransportConfig"
            )

    def to_dict(self) -> dict:
        """将实体转换为字典（用于 YAML 序列化）。

        不包含 service_id，因为 service_id 是文件名。
        """
        data: dict = {
            "name": self.name,
            "transport": self.transport.to_dict(),
            "enabled": self.enabled,
        }
        if self.description:
            data["description"] = self.description
        if self.usage_guide:
            data["usage_guide"] = self.usage_guide
        return data

    @classmethod
    def from_dict(cls, service_id: str, data: dict) -> McpServiceDefinition:
        """从字典（YAML 反序列化结果）创建实体。

        Args:
            service_id: 服务唯一标识（来自文件名）。
            data: YAML 文件解析后的字典。

        Returns:
            McpServiceDefinition 实例。
        """
        transport_data = data.get("transport", {})
        transport_type = transport_data.get("type", "stdio") if isinstance(transport_data, dict) else transport_data

        if transport_type == "stdio":
            transport_dict = transport_data if isinstance(transport_data, dict) else {}
            transport: TransportConfig = StdioTransportConfig.from_dict(transport_dict)
        elif transport_type == "streamable-http":
            transport_dict = transport_data if isinstance(transport_data, dict) else {}
            transport = StreamableHttpTransportConfig.from_dict(transport_dict)
        else:
            raise ValueError(f"不支持的传输方式: {transport_type}")

        return cls(
            service_id=service_id,
            name=data.get("name", ""),
            transport=transport,
            description=data.get("description"),
            enabled=data.get("enabled", True),
            usage_guide=data.get("usage_guide"),
        )
