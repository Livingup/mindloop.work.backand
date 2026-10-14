# -*- coding: utf-8 -*-
"""Agent 定义服务配置。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentDefinitionConfig:
    """Agent 定义服务配置。

    Agent 定义是本应用用户数据，默认存储在全局用户数据目录 ~/.spread-ai/agents 下。
    该配置只保存定义文件根目录，仓储通过 Callable 访问器实时读取该目录以支持配置热更新。
    """

    # Agent 定义 YAML 文件存储目录。每个定义一个文件：{agent_id}.yaml。
    definitions_dir: str
