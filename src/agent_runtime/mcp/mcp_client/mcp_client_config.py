# -*- coding: utf-8 -*-
"""MCP 客户端子模块配置。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class McpClientConfig:
    """MCP 客户端服务配置。"""

    # MCP 服务配置 YAML 文件存放目录（每个文件对应一个可连接的 MCP 服务）。
    config_dir: str
