# -*- coding: utf-8 -*-
"""Chat Bubble 前端消息气泡模块工厂。"""
from __future__ import annotations

from threading import RLock

from agent_runtime.agent.agent_runtime_config import AgentRuntimeStateConfig
from agent_runtime.chat_bubble.chat_bubble_service import ChatBubbleService
from agent_runtime.chat_bubble.infrastructure.sqlite_chat_bubble_repository import (
    SqliteChatBubbleRepository,
)


class ChatBubbleFactory:
    """Chat Bubble 模块组合根。

    工厂负责读取 AgentRuntime 运行态数据目录，装配 SQLite Chat Bubble 仓储和
    ChatBubbleService。Chat Bubble 是前端 UI 展示状态，生命周期由前端 REST API 驱动，
    但它需要与 AgentRuntime 的 scene/root instance 使用同一运行态数据目录，便于服务
    重启后恢复同一个 Scene 的历史消息气泡和 pending 用户消息浮层。
    """

    _lock = RLock()
    _service: ChatBubbleService | None = None
    _config_signature: tuple[str] | None = None

    @classmethod
    def create(cls) -> ChatBubbleService:
        """创建或返回进程内共享的 Chat Bubble 服务。"""
        with cls._lock:
            config = cls._read_config()
            signature = (config.data_dir,)
            if cls._service is None or cls._config_signature != signature:
                data_dir_accessor = cls._build_data_dir_accessor(config)
                repository = SqliteChatBubbleRepository(data_dir_accessor=data_dir_accessor)
                cls._service = ChatBubbleService(repository)
                cls._config_signature = signature
            return cls._service

    @staticmethod
    def _read_config() -> AgentRuntimeStateConfig:
        """读取 AgentRuntime 运行态配置；ConfigRegistry 未初始化时使用默认配置。"""
        try:
            from agent_runtime.config import ConfigRegistry

            return ConfigRegistry.instance().agent_runtime
        except RuntimeError:
            return AgentRuntimeStateConfig()

    @staticmethod
    def _build_data_dir_accessor(config: AgentRuntimeStateConfig):
        """构建运行态数据目录访问器，支持 ConfigRegistry 热更新后切换目录。"""
        try:
            from agent_runtime.config import ConfigRegistry

            ConfigRegistry.instance()
        except RuntimeError:
            return lambda: config.data_dir
        return lambda: ConfigRegistry.instance().agent_runtime.data_dir
