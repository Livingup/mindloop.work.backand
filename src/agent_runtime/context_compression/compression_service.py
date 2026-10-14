# -*- coding: utf-8 -*-
"""上下文压缩服务 (Context Compression Service).

注意：
系统已全面迁移至基于观察者与增量切片的 Task Note 架构 (TaskNoteStore / TaskNoteUnitSlicer / TaskNoteContextAssembler)。
原有的旧版多 Worker（6 Worker 并发抽取、拼接及重写）方案已完全废弃并移除。
本服务保留最小必要门禁配置访问器与接口，以维持与 AgentExecutionEngine / AgentExecutionFactory 的装配兼容性。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from agent_runtime.context_compression.config import ContextCompressionConfig
from agent_runtime.context_compression.gate import decide_context_compression
from agent_runtime.context_compression.models import CompressionDecision, CompressionResult
from agent_runtime.context_compression.settings_repository import ContextCompressionSettingsRepository

if TYPE_CHECKING:
    from agent_runtime.agent.agent_instance import AgentInstance
    from agent_runtime.agent.agent_task import AgentTask
    from agent_runtime.communication.runtime_event_publisher import RuntimeCommunicationContext
    from agent_runtime.conversation_message.message.models import Conversation
    from agent_runtime.llm_model_registry.llm_model_registry_service import ResolvedLlmProviderModel

logger = logging.getLogger(__name__)


class ContextCompressionService:
    """上下文压缩服务门禁与配置托管类。"""

    def __init__(
        self,
        *,
        config_accessor: Callable[[], ContextCompressionConfig],
        settings_repository: ContextCompressionSettingsRepository | None = None,
        **kwargs: Any,
    ) -> None:
        self._config_accessor = config_accessor
        self._settings_repository = settings_repository
        # 吸收其余兼容装配参数（如 archive_repository, event_publisher 等）
        self._extra_deps = kwargs

    def get_effective_config(self) -> ContextCompressionConfig:
        """获取当前生效的上下文压缩配置。"""
        return replace(self._config_accessor(), input_mode="native_multimodal_history")

    def check_compression_decision(
        self,
        conversation: Conversation,
        resolved_model: ResolvedLlmProviderModel,
    ) -> CompressionDecision:
        """检查会话是否达到压缩门禁阈值。"""
        config = self.get_effective_config()
        return decide_context_compression(conversation, resolved_model, config)

    async def compress_if_needed(
        self,
        *,
        task: AgentTask,
        instance: AgentInstance,
        source_conversation: Conversation,
        trigger_model: ResolvedLlmProviderModel,
        communication_context: RuntimeCommunicationContext | None = None,
        interrupt_checker: Callable[[], bool] | None = None,
        request_canceller_observer: Callable[[Callable[[], None]], Callable[[], None] | None] | None = None,
    ) -> CompressionResult:
        """兼容桩：多 Worker 方案已迁移为 Task Note 方案，此处始终返回未触发。"""
        del task, instance, source_conversation, trigger_model, communication_context
        del interrupt_checker, request_canceller_observer
        return CompressionResult(
            applied=False,
            reason="migrated_to_task_note",
            status="skipped",
        )
