# -*- coding: utf-8 -*-
"""Agent 执行循环引擎。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_runtime.agent.agent_instance import AgentInstance
from agent_runtime.agent.agent_trace_recorder import AgentTraceIdentity, AgentTraceRecorder
from agent_runtime.agent.north_gateway.agent_runtime_contracts import (
    AgentRuntimeGateway,
    CreateTaskWithRootAgentInstanceRequest,
)
from agent_runtime.agent.tool_dispatch.tool_dispatcher import AgentToolDispatcher
from agent_runtime.agent_definition.agent_definition import AgentDefinition
from agent_runtime.agent_definition.north_gateway.agent_definition_contracts import (
    AgentDefinitionNorthGateway,
)
from agent_runtime.conversation_message.conversation_repository import ConversationRepository
from agent_runtime.conversation_message.message.models import (
    PartMeta,
    TextPart,
    TextPartMeta,
    ToolUsePart,
    UniMessage,
    UniPart,
    ROLE_TOOL,
)
from agent_runtime.conversation_message.message.operations import create_conversation
from agent_runtime.llm_client.llm_request_models import LlmPromptRequest
from agent_runtime.llm_client.llm_request_service import LlmRequestService
from agent_runtime.llm_model_registry.north_gateway.llm_model_registry_contracts import (
    LlmModelRegistryGateway,
)
from agent_runtime.prompt_template.north_gateway.prompt_snippet_contracts import (
    PromptSnippetNorthGateway,
)


@dataclass(frozen=True, slots=True)
class AgentExecutionRequest:
    """启动根 Agent 执行循环请求。"""

    # 所属线程 ID。线程用于隔离同一任务内不同对话上下文。
    thread_id: str

    # 根 Agent 静态定义 ID。
    agent_definition_id: str

    # 本次执行使用的 LLM 服务商 ID。
    llm_provider_id: str

    # 本次执行使用的服务商模型接入项 ID。
    llm_model_binding_id: str

    # 用户输入内容块。纯文本场景可使用 from_text 构造。
    user_parts: tuple[UniPart, ...]

    # 用户输入内容块元数据。图片/PDF 场景调用方应传入准确页数等信息。
    user_parts_meta: tuple[PartMeta, ...]

    # 可选 task_id。不传时由 AgentRuntimeGateway 生成。
    task_id: str | None = None

    # 可选 root instance id。不传时由 AgentRuntimeGateway 生成。
    root_instance_id: str | None = None

    # 已有 conversation_id。为空时执行器新建 conversation。
    conversation_id: str | None = None

    # 最大工具调用轮次，防止工具调用死循环。不传时使用 agent_execution.max_tool_rounds 配置默认值。
    max_tool_rounds: int | None = None

    # render_messages 透传选项。
    render_options: dict[str, Any] = field(default_factory=dict)

    # LLM 请求额外覆盖参数。
    request_overrides: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_text(
        cls,
        *,
        thread_id: str,
        agent_definition_id: str,
        llm_provider_id: str,
        llm_model_binding_id: str,
        user_text: str,
        task_id: str | None = None,
        root_instance_id: str | None = None,
        conversation_id: str | None = None,
        max_tool_rounds: int | None = None,
        render_options: dict[str, Any] | None = None,
        request_overrides: dict[str, Any] | None = None,
    ) -> "AgentExecutionRequest":
        """创建纯文本用户输入的执行请求。"""
        return cls(
            thread_id=thread_id,
            agent_definition_id=agent_definition_id,
            llm_provider_id=llm_provider_id,
            llm_model_binding_id=llm_model_binding_id,
            user_parts=(TextPart(text=user_text),),
            user_parts_meta=(TextPartMeta(),),
            task_id=task_id,
            root_instance_id=root_instance_id,
            conversation_id=conversation_id,
            max_tool_rounds=max_tool_rounds,
            render_options=render_options or {},
            request_overrides=request_overrides or {},
        )


@dataclass(frozen=True, slots=True)
class AgentExecutionResult:
    """根 Agent 执行结果。"""

    # 任务 ID。
    task_id: str

    # 根 Agent 实例 ID。
    instance_id: str

    # 会话 ID。
    conversation_id: str

    # Trace 文件绝对路径。
    trace_file_path: str

    # 最后一条 assistant 消息。
    assistant_message: UniMessage | None


class AgentExecutionEngine:
    """真正 Agent 执行循环。

    引擎串联运行态管理、静态 Agent 定义、Prompt Snippet、Conversation、LLM 请求客户端和工具调度器。
    本类不提供 REST/CLI 管理接口，只作为程序内部运行入口。
    """

    def __init__(
        self,
        *,
        runtime_gateway: AgentRuntimeGateway,
        agent_definition_gateway: AgentDefinitionNorthGateway,
        llm_registry_gateway: LlmModelRegistryGateway,
        conversation_repository: ConversationRepository,
        prompt_snippet_gateway: PromptSnippetNorthGateway,
        llm_request_service: LlmRequestService,
        tool_dispatcher: AgentToolDispatcher,
        trace_recorder: AgentTraceRecorder,
        default_max_tool_rounds: int = 20,
    ) -> None:
        # Agent 运行态网关，负责 task/instance 创建和状态流转。
        self._runtime_gateway = runtime_gateway
        # 静态 Agent 定义网关，负责读取系统提示词、工具 allowlist、MCP allowlist 等执行边界。
        self._agent_definition_gateway = agent_definition_gateway
        # LLM 注册网关，负责解析 provider/binding/model metadata 为最终调用配置。
        self._llm_registry_gateway = llm_registry_gateway
        # Conversation 仓储，负责历史消息持久化。
        self._conversation_repository = conversation_repository
        # Prompt Snippet 网关，用于解析 AgentDefinition.system_prompt 中的 template_id。
        self._prompt_snippet_gateway = prompt_snippet_gateway
        # 通用 LLM 请求服务，负责真实请求和响应归一化。
        self._llm_request_service = llm_request_service
        # 工具调度器，负责工具 schema 构建和工具调用执行。
        self._tool_dispatcher = tool_dispatcher
        # Agent 实例 Markdown Trace 记录器。
        self._trace_recorder = trace_recorder
        # 默认最大工具调用轮次。请求未显式指定时使用该值，防止工具调用死循环。
        if default_max_tool_rounds < 0:
            raise ValueError("default_max_tool_rounds 必须是非负整数。")
        self._default_max_tool_rounds = default_max_tool_rounds

    async def run_root_agent(self, request: AgentExecutionRequest) -> AgentExecutionResult:
        """创建 task + root Agent 实例并执行完整对话循环。"""
        task, instance = self._runtime_gateway.create_task_with_root_agent_instance(
            CreateTaskWithRootAgentInstanceRequest(
                thread_id=request.thread_id,
                agent_definition_id=request.agent_definition_id,
                llm_provider_id=request.llm_provider_id,
                llm_model_binding_id=request.llm_model_binding_id,
                task_id=request.task_id,
                root_instance_id=request.root_instance_id,
                conversation_id=request.conversation_id,
            )
        )
        trace_path = self._trace_recorder.start_trace(_trace_identity(instance))
        try:
            self._runtime_gateway.start_task(task.task_id)
            self._runtime_gateway.start_agent_instance(instance.instance_id)
            agent_definition = self._load_agent_definition(instance.agent_definition_id)
            conversation = await self._load_or_create_conversation(instance, request)

            conversation.add_user_message(list(request.user_parts), list(request.user_parts_meta))
            user_message = conversation.messages[-1]
            await self._conversation_repository.save(conversation)
            self._trace_recorder.record_user_message(trace_path, user_message)

            system_prompt = self._render_system_prompt(agent_definition)
            resolved_model = self._llm_registry_gateway.resolve_provider_model(
                instance.llm_provider_id,
                instance.llm_model_binding_id,
            )
            tools = await self._tool_dispatcher.build_llm_tools(
                agent_definition=agent_definition,
                sdk_platform=resolved_model.sdk_platform,
            )

            max_tool_rounds = (
                request.max_tool_rounds
                if request.max_tool_rounds is not None
                else self._default_max_tool_rounds
            )

            for round_index in range(max_tool_rounds + 1):
                llm_response = await self._llm_request_service.send_prompt(
                    LlmPromptRequest(
                        conversation=conversation,
                        resolved_model=resolved_model,
                        system_prompt=system_prompt,
                        tools=tools,
                        render_options=request.render_options,
                        request_overrides=request.request_overrides,
                    )
                )
                assistant_message = llm_response.assistant_message
                await self._conversation_repository.save(conversation)
                self._trace_recorder.record_llm_response(trace_path, assistant_message)

                tool_uses = _extract_tool_uses(assistant_message)
                if not tool_uses:
                    self._runtime_gateway.complete_agent_instance(instance.instance_id)
                    self._runtime_gateway.complete_task(task.task_id)
                    self._trace_recorder.record_completed(trace_path)
                    return AgentExecutionResult(
                        task_id=task.task_id,
                        instance_id=instance.instance_id,
                        conversation_id=conversation.id,
                        trace_file_path=str(trace_path),
                        assistant_message=assistant_message,
                    )

                if round_index >= max_tool_rounds:
                    raise RuntimeError(f"工具调用轮次超过上限: {max_tool_rounds}")

                tool_result_message = await self._execute_tool_uses(
                    agent_definition,
                    tool_uses,
                )
                conversation.append_message(tool_result_message)
                await self._conversation_repository.save(conversation)
                self._trace_recorder.record_tool_result(trace_path, tool_result_message)

            raise RuntimeError("Agent 执行循环异常结束。")
        except Exception as exc:
            failure_message = str(exc)
            self._runtime_gateway.fail_agent_instance(instance.instance_id, failure_message)
            self._runtime_gateway.fail_task(task.task_id, failure_message)
            self._trace_recorder.record_failure(trace_path, failure_message)
            raise

    def _load_agent_definition(self, agent_definition_id: str) -> AgentDefinition:
        """加载并校验 Agent 静态定义。"""
        definition = self._agent_definition_gateway.get_agent_definition(agent_definition_id)
        if definition.disabled:
            raise ValueError(f"Agent 定义已禁用，不能执行: {agent_definition_id}")
        return definition

    async def _load_or_create_conversation(
        self,
        instance: AgentInstance,
        request: AgentExecutionRequest,
    ):
        """加载已有会话或为当前 Agent 实例创建新会话。"""
        if request.conversation_id:
            return await self._conversation_repository.get(request.conversation_id)
        conversation = create_conversation()
        await self._conversation_repository.create(
            conversation,
            task_id=instance.task_id,
            thread_id=instance.thread_id,
            agent_id=instance.agent_definition_id,
            agent_instance_id=instance.instance_id,
        )
        self._runtime_gateway.bind_agent_instance_conversation(
            instance.instance_id,
            conversation.id,
        )
        return conversation

    def _render_system_prompt(self, definition: AgentDefinition) -> str | None:
        """按 AgentDefinition.system_prompt 顺序拼接最终系统提示词。"""
        segments: list[str] = []
        for segment in definition.system_prompt:
            if segment.text is not None:
                segments.append(segment.text)
            elif segment.template_id is not None:
                snippet = self._prompt_snippet_gateway.get_snippet(segment.template_id)
                segments.append(snippet.text)
        text = "\n\n".join(item for item in segments if item.strip())
        return text or None

    async def _execute_tool_uses(
        self,
        definition: AgentDefinition,
        tool_uses: list[ToolUsePart],
    ) -> UniMessage:
        """执行 assistant 消息中的全部工具调用并生成 tool 消息。"""
        result_parts = []
        for tool_use in tool_uses:
            result = await self._tool_dispatcher.dispatch_tool_call(
                agent_definition=definition,
                tool_use=tool_use,
            )
            result_parts.append(result.result_part)
        return UniMessage(role=ROLE_TOOL, content=result_parts)


def _extract_tool_uses(message: UniMessage) -> list[ToolUsePart]:
    """从 assistant 消息中提取工具调用。"""
    return [part for part in message.content if isinstance(part, ToolUsePart)]


def _trace_identity(instance: AgentInstance) -> AgentTraceIdentity:
    """从 AgentInstance 构造 Trace 文件身份信息。"""
    return AgentTraceIdentity(
        task_id=instance.task_id,
        instance_id=instance.instance_id,
        thread_id=instance.thread_id,
        agent_definition_id=instance.agent_definition_id,
        trace_id=instance.trace_id,
    )
