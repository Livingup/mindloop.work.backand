"""系统支持的用户输入模态定义。

本模块只维护 Agent Runtime 当前支持的输入模态集合。
这些值表示系统消息层和模型元数据层共同认可的用户输入能力，
不是用户动态数据，也不是部署配置项。
"""
from __future__ import annotations

from typing import Final


# 文本输入模态。所有 LLM 文本对话都应至少支持该模态。
INPUT_MODALITY_TEXT: Final[str] = "text"

# 图片输入模态。对应统一文件资源服务中的图片资源引用。
INPUT_MODALITY_IMAGE: Final[str] = "image"

# PDF 输入模态。对应统一文件资源服务中的 PDF 资源引用。
INPUT_MODALITY_PDF: Final[str] = "pdf"

# 当前系统支持的全部用户输入模态。模型元数据只能从该集合中声明自身支持项。
SUPPORTED_INPUT_MODALITIES: Final[frozenset[str]] = frozenset({
    INPUT_MODALITY_TEXT,
    INPUT_MODALITY_IMAGE,
    INPUT_MODALITY_PDF,
})
