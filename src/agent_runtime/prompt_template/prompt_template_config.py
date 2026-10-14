"""提示词模板服务模块配置。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PromptTemplateConfig:
    """提示词模板服务配置。"""

    # 提示词片段 YAML 文件存储根目录（.spread-ai/prompt-snippets/）。
    snippets_dir: str
