# -*- coding: utf-8 -*-
"""LLM 模型注册模块配置。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LlmModelRegistryConfig:
    """LLM 服务商与模型注册模块配置。

    该配置只保存用户自定义注册数据的根目录位置。运行期 SQLite 数据库文件由
    llm_model_registry 模块在该目录下解析，不把具体模型、服务商或策略数据写死在系统主配置中。
    """

    # LLM 模型、服务商与执行策略注册数据根目录；SQLite 文件名为 llm_model_registry.sqlite3。
    registry_dir: str
