# -*- coding: utf-8 -*-
"""通用 LLM 请求客户端内部请求与结果模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_runtime.conversation_message.message.models import Conversation, UniMessage
from agent_runtime.llm_model_registry.llm_model_registry_service import (
    ResolvedLlmProviderModel,
)


@dataclass(frozen=True, slots=True)
class LlmToolDefinition:
    """一次 LLM 请求可用的工具定义。

    schema 是已经转换到当前 SDK 平台可识别格式的工具声明。
    调度器在执行前仍会按 AgentDefinition 的 allowlist 再次校验权限。
    """

    # LLM 看到并返回的工具名称。
    name: str

    # 当前 sdk_platform 下的工具 schema。
    schema: dict[str, Any]

    # 工具来源：local 或 mcp。
    source: str = "local"

    # MCP 服务 ID。source=mcp 时有值。
    mcp_service_id: str | None = None

    # MCP 原始工具名。source=mcp 时有值。
    mcp_tool_name: str | None = None


@dataclass(frozen=True, slots=True)
class LlmPromptRequest:
    """通用 LLM 单轮请求。

    上层 Agent 执行循环传入已加载的 Conversation、解析后的 provider/binding 配置、系统提示词文本和工具定义。
    LLM 客户端负责把这些信息与 conversation_message 的协议渲染结果合并后发起真实请求。
    """

    # 当前 Agent 实例的完整历史会话。
    conversation: Conversation

    # LLM 注册模块解析出的最终调用配置快照。
    resolved_model: ResolvedLlmProviderModel

    # 本轮请求使用的系统提示词。Trace 不会记录该字段。
    system_prompt: str | None = None

    # 当前 AgentDefinition 允许暴露给 LLM 的工具声明。
    tools: tuple[LlmToolDefinition, ...] = field(default_factory=tuple)

    # render_messages 的可选参数，如 OpenAI Responses 的 stored。
    render_options: dict[str, Any] = field(default_factory=dict)

    # 本轮请求额外参数，会覆盖 registry 中 custom_request_params 的同名字段。
    request_overrides: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LlmPromptResponse:
    """通用 LLM 请求结果。"""

    # 原始响应 JSON 对象。
    raw_response: Any

    # 已经由 conversation_message 归一化并追加到 Conversation 的 assistant 消息。
    assistant_message: UniMessage

    # 本次实际使用的 API Key 序号，用于排障；不暴露 key 明文。
    api_key_index: int | None = None

    # 本次请求尝试次数。
    attempt_count: int = 1
