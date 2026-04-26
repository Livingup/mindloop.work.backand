# -*- coding: utf-8 -*-
"""LLM 模型注册模块配置。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LlmModelRegistryConfig:
    """LLM 服务商与模型注册模块配置。

    该配置只保存用户自定义注册数据的根目录位置。具体模型元数据和服务商配置
    由 llm_model_registry 模块自己的 YAML 仓储按需读写，不写死在系统主配置中。
    """

    # LLM 模型与服务商注册数据根目录。目录下按 models/ 和 providers/ 拆分存储 YAML 文件。
    registry_dir: str
