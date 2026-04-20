"""统一 LLM 域配置模型。

各域配置 dataclass 归各域所有，顶层 ConfigRegistry 聚合引用。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ConversationRepositoryConfig:
    """会话消息持久化仓库配置。"""

    # 会话数据存储根目录（SQLite + JSONL 消息文件）。
    data_dir: str
