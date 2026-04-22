# -*- coding: utf-8 -*-
"""MCP 传输配置值对象。

描述 MCP 服务的传输方式和连接参数。
作为 McpServiceDefinition 的嵌套组件，按传输方式区分为不同的值对象。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class StdioTransportConfig:
    """stdio 传输配置。

    通过启动子进程并用 stdin/stdout 通信。
    """

    command: str
    """启动命令（如 npx、python、node）。"""

    args: list[str] = field(default_factory=list)
    """命令参数列表。"""

    env: dict[str, str] = field(default_factory=dict)
    """进程额外环境变量。"""

    def to_dict(self) -> dict:
        """序列化为字典。"""
        data: dict = {"type": "stdio", "command": self.command}
        if self.args:
            data["args"] = self.args
        if self.env:
            data["env"] = self.env
        return data

    @classmethod
    def from_dict(cls, data: dict) -> StdioTransportConfig:
        """从字典反序列化。"""
        return cls(
            command=data["command"],
            args=data.get("args", []),
            env=data.get("env", {}),
        )


@dataclass
class StreamableHttpTransportConfig:
    """streamable-http 传输配置。

    通过 HTTP 长连接 + SSE 与远程 MCP 服务通信。
    """

    url: str
    """服务端 URL。"""

    headers: dict[str, str] = field(default_factory=dict)
    """自定义请求头（如 Authorization）。"""

    def to_dict(self) -> dict:
        """序列化为字典。"""
        data: dict = {"type": "streamable-http", "url": self.url}
        if self.headers:
            data["headers"] = self.headers
        return data

    @classmethod
    def from_dict(cls, data: dict) -> StreamableHttpTransportConfig:
        """从字典反序列化。"""
        return cls(
            url=data["url"],
            headers=data.get("headers", {}),
        )


# 传输配置联合类型
TransportConfig = StdioTransportConfig | StreamableHttpTransportConfig
