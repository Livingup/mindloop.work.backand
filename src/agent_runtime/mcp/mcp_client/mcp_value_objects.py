# -*- coding: utf-8 -*-
"""MCP 客户端值对象 — 运行时从 MCP 服务获取的数据模型。

这些值对象不持久化，没有唯一标识，用于在各层之间传递 MCP 协议数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class McpServiceCapabilities:
    """MCP 服务能力 — initialize 响应中协商的能力信息。"""

    supports_tools: bool = False
    """是否支持 tools 能力。"""

    supports_resources: bool = False
    """是否支持 resources 能力。"""

    server_name: str = ""
    """服务端名称（来自 serverInfo.name）。"""

    server_version: str = ""
    """服务端版本（来自 serverInfo.version）。"""

    instructions: str | None = None
    """服务级使用指南（来自 initialize 响应的 instructions 字段）。"""


@dataclass(frozen=True)
class McpToolInfo:
    """MCP 工具信息 — 包含完整的 inputSchema。"""

    name: str
    """工具名称。"""

    description: str | None = None
    """工具描述。"""

    input_schema: dict = field(default_factory=dict)
    """工具入参的 JSON Schema（标准 JSON Schema 2020-12）。"""


@dataclass(frozen=True)
class McpToolSummary:
    """MCP 工具摘要 — 仅名称和描述，不含 schema。"""

    name: str
    """工具名称。"""

    description: str | None = None
    """工具描述。"""


@dataclass(frozen=True)
class McpResourceInfo:
    """MCP 资源信息。"""

    uri: str
    """资源 URI。"""

    name: str
    """资源名称。"""

    description: str | None = None
    """资源描述。"""

    mime_type: str | None = None
    """资源 MIME 类型。"""


@dataclass(frozen=True)
class McpResourceContent:
    """MCP 资源内容 — read_resource 的返回结果。"""

    uri: str
    """资源 URI。"""

    mime_type: str | None = None
    """资源 MIME 类型。"""

    text: str | None = None
    """文本内容（TextResourceContents）。"""

    blob: str | None = None
    """二进制内容的 base64 编码（BlobResourceContents）。"""


@dataclass(frozen=True)
class McpToolResult:
    """MCP 工具执行结果。"""

    content: str = ""
    """工具返回的文本内容（多个 TextContent 拼接）。"""

    structured_content: dict | None = None
    """工具返回的结构化内容。"""

    is_error: bool = False
    """工具执行是否报错。"""
