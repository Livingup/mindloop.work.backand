# -*- coding: utf-8 -*-
"""MCP 客户端子模块配置。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class McpClientConfig:
    """MCP 客户端服务配置。"""

    # MCP 服务定义 YAML 文件存放目录（每个服务一个 YAML 文件）。
    config_dir: str
