# -*- coding: utf-8 -*-
"""通用通信模块配置。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CommunicationConfig:
    """通用通信模块基础设施配置。

    当前仅描述通信流与消息日志的 SQLite 数据目录。实时推送通道类型由 REST/SSE 适配器决定，
    不写入业务对象，避免 Agent 执行层感知具体传输技术。
    """

    # 通信流和消息日志 SQLite 数据目录。
    data_dir: str = "~/.spread-ai/communication"

    def __post_init__(self) -> None:
        """校验通信模块配置合法性。"""
        if not isinstance(self.data_dir, str) or not self.data_dir.strip():
            raise ValueError("communication.data_dir 不能为空。")
