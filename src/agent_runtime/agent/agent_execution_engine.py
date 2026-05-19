# -*- coding: utf-8 -*-
"""Agent 执行循环引擎。"""
from __future__ import annotations

import asyncio
import os
import platform
import logging
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, TypeVar

from agent_runtime.agent.agent_instance import (
    AGENT_INSTANCE_STATUS_RUNNING,
    AgentInstance,
    PendingAgentUserMessage,
    PendingUserMessageAttachment,
)
from agent_runtime.agent.agent_task import AgentTask
from agent_runtime.agent.agent_trace_recorder import AgentTraceIdentity, AgentTraceRecorder
from agent_runtime.agent.north_gateway.agent_runtime_contracts import (
    AgentRuntimeGateway,
    CreateTaskWithRootAgentInstanceRequest,
)
from agent_runtime.agent.tool_dispatch.tool_dispatcher import AgentToolDispatcher, ToolDispatchResult
from agent_runtime.agent_definition.agent_definition import AgentDefinition
from agent_runtime.agent_definition.north_gateway.agent_definition_contracts import (
    AgentDefinitionNorthGateway,
)
from agent_runtime.communication.north_gateway.communication_contracts import (
    CommunicationGateway,
    CreateCommunicationStreamRequest,
)
from agent_runtime.communication.runtime_event_publisher import (
    NoopRuntimeEventPublisher,
    RuntimeCommunicationContext,
    RuntimeEventPublisher,
)
from agent_runtime.conversation_message.conversation_repository import ConversationRepository
from agent_runtime.conversation_message.message.models import (
    PartMeta,
    ImagePart,
    ImagePartMeta,
    PdfPart,
    PdfPartMeta,
    TextPart,
    TextPartMeta,
    ToolResultPart,
    ToolUsePart,
    UniMessage,
    UniPart,
    ROLE_TOOL,
)
from agent_runtime.conversation_message.message.operations import create_conversation
from agent_runtime.input_modality import INPUT_MODALITY_IMAGE, INPUT_MODALITY_PDF
from agent_runtime.llm_client.llm_request_models import LlmPromptRequest, LlmPromptResponse
from agent_runtime.llm_client.llm_request_service import (
    LlmProviderModelServiceUnavailable,
    LlmRequestService,
)
from agent_runtime.llm_model_registry.llm_execution_policy import (
    UNSUPPORTED_INPUT_MODALITY_STRATEGY_DEGRADE,
    UNSUPPORTED_INPUT_MODALITY_STRATEGY_SKIP_CANDIDATE,
)
from agent_runtime.llm_model_registry.llm_model_registry_service import (
    ResolvedLlmProviderModel,
)
from agent_runtime.llm_model_registry.north_gateway.llm_model_registry_contracts import (
    LlmModelRegistryGateway,
)
from agent_runtime.mcp.mcp_client.north_gateway.mcp_client_contracts import McpClientGateway
from agent_runtime.prompt_template.north_gateway.prompt_snippet_contracts import (
    PromptSnippetNorthGateway,
)
from agent_runtime.skill.north_gateway.skill_contracts import SkillGateway
from agent_runtime.tool.north_gateway.local_tool_contracts import LocalToolGateway
from agent_runtime.tool.tool_execution_models import BaseToolResult, LoopDecision, build_tool_error_result


_PLAIN_TEXT_FALLBACK_PAUSE_THRESHOLD = 5
_AGENT_STAGE_PROGRESS_LOG_INTERVAL_SECONDS = 30.0
logger = logging.getLogger(__name__)
_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class AgentExecutionRequest:
    """启动根 Agent 执行循环请求。"""

    # 所属线程 ID。线程用于隔离同一任务内不同对话上下文。
    thread_id: str

    current_working_directory: str

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

    # render_messages 透传选项。
    render_options: dict[str, Any] = field(default_factory=dict)

    # LLM 请求额外覆盖参数。
    request_overrides: dict[str, Any] = field(default_factory=dict)

    # 可选通信流 ID。前端或 CLI UI 可先创建/订阅该流，执行器将实时事件发布到该逻辑流。
    communication_stream_id: str | None = None

    client_message_id: str | None = None

    @classmethod
    def from_text(
        cls,
        *,
        thread_id: str,
        current_working_directory: str,
        agent_definition_id: str,
        llm_provider_id: str,
        llm_model_binding_id: str,
        user_text: str,
        task_id: str | None = None,
        root_instance_id: str | None = None,
        conversation_id: str | None = None,
        render_options: dict[str, Any] | None = None,
        request_overrides: dict[str, Any] | None = None,
        communication_stream_id: str | None = None,
        client_message_id: str | None = None,
    ) -> "AgentExecutionRequest":
        """创建纯文本用户输入的执行请求。"""
        return cls(
            thread_id=thread_id,
            current_working_directory=current_working_directory,
            agent_definition_id=agent_definition_id,
            llm_provider_id=llm_provider_id,
            llm_model_binding_id=llm_model_binding_id,
            user_parts=(TextPart(text=user_text),),
            user_parts_meta=(TextPartMeta(),),
            task_id=task_id,
            root_instance_id=root_instance_id,
            conversation_id=conversation_id,
            render_options=render_options or {},
            request_overrides=request_overrides or {},
            communication_stream_id=communication_stream_id,
            client_message_id=client_message_id,
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

    # 本次执行关联的逻辑通信流 ID。前端/CLI UI 通过该 ID 订阅实时事件。
    communication_stream_id: str | None = None


@dataclass(frozen=True, slots=True)
class AgentMessageSubmissionResult:
    status: str
    task_id: str
    instance_id: str
    conversation_id: str | None
    communication_stream_id: str
    # 后端为本次用户输入生成的待处理消息 ID；SSE user.message.accepted 会携带同一 ID。
    pending_message_id: str
    # 前端提交的客户端消息 ID；用于前端在 HTTP 响应和 SSE 接收事件之间做 UI 对齐。
    client_message_id: str | None
    # True 表示消息仅进入等待队列，需在浮层展示；False 表示已启动执行循环，可按正常 chat 消息展示。
    queued_for_later: bool
    # 当前 root agent instance 快照。Scene root instance 会跨 task 复用，
    # 当后端自动创建下一轮 task 时，前端需要用该快照同步 task_id/thread_id/trace_id。
    instance: AgentInstance


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
        local_tool_gateway: LocalToolGateway,
        mcp_client_gateway: McpClientGateway | None,
        skill_gateway: SkillGateway | None,
        llm_request_service: LlmRequestService,
        tool_dispatcher: AgentToolDispatcher,
        trace_recorder: AgentTraceRecorder,
        communication_gateway: CommunicationGateway | None = None,
        event_publisher: RuntimeEventPublisher | None = None,
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
        self._local_tool_gateway = local_tool_gateway
        self._mcp_client_gateway = mcp_client_gateway
        self._skill_gateway = skill_gateway
        # 通用 LLM 请求服务，负责真实请求和响应归一化。
        self._llm_request_service = llm_request_service
        # 工具调度器，负责工具 schema 构建和工具调用执行。
        self._tool_dispatcher = tool_dispatcher
        # Agent 实例 Markdown Trace 记录器。
        self._trace_recorder = trace_recorder
        # 通用通信网关。用于在未传 communication_stream_id 时自动创建 agent.task 逻辑流。
        self._communication_gateway = communication_gateway
        # 运行时事件发布器。所有 Agent/LLM/Tool 关键节点都通过逻辑 stream_id 发布。
        self._event_publisher = event_publisher or NoopRuntimeEventPublisher()
        self._background_tasks: set[asyncio.Task[AgentExecutionResult]] = set()
        self._active_instance_ids: set[str] = set()

    async def run_root_agent(self, request: AgentExecutionRequest) -> AgentExecutionResult:
        """创建 task + root Agent 实例并执行完整对话循环。"""
        task, instance = self._runtime_gateway.create_task_with_root_agent_instance(
            CreateTaskWithRootAgentInstanceRequest(
                thread_id=request.thread_id,
                current_working_directory=request.current_working_directory,
                agent_definition_id=request.agent_definition_id,
                llm_provider_id=request.llm_provider_id,
                llm_model_binding_id=request.llm_model_binding_id,
                task_id=request.task_id,
                root_instance_id=request.root_instance_id,
                conversation_id=request.conversation_id,
            )
        )
        self._runtime_gateway.enqueue_agent_instance_user_message(
            instance.instance_id,
            user_text=_request_user_text(request),
            attachments=_request_user_attachments(request),
            client_message_id=request.client_message_id,
        )
        instance = self._runtime_gateway.get_agent_instance(instance.instance_id)
        return await self._run_agent_instance(task, instance, request)

    async def run_agent_instance_message(
        self,
        *,
        agent_instance_id: str,
        user_text: str,
        attachments: tuple[PendingUserMessageAttachment, ...] = (),
        render_options: dict[str, Any] | None = None,
        request_overrides: dict[str, Any] | None = None,
        communication_stream_id: str | None = None,
    ) -> AgentExecutionResult:
        instance = self._runtime_gateway.get_agent_instance(agent_instance_id)
        task = self._runtime_gateway.get_task(instance.task_id)
        if task.is_terminal:
            if task.scene_id is None:
                raise ValueError(f"Agent 任务已结束，不能继续对话: {task.task_id}")
            task, instance = self._runtime_gateway.prepare_scene_root_agent_instance_for_new_task(
                instance.instance_id,
            )
        instance, _pending_message = self._runtime_gateway.enqueue_agent_instance_user_message(
            instance.instance_id,
            user_text=user_text,
            attachments=attachments,
        )
        request = AgentExecutionRequest.from_text(
            thread_id=instance.thread_id,
            current_working_directory=instance.current_working_directory,
            agent_definition_id=instance.agent_definition_id,
            llm_provider_id=instance.llm_provider_id,
            llm_model_binding_id=instance.llm_model_binding_id,
            user_text=user_text,
            task_id=task.task_id,
            root_instance_id=instance.instance_id,
            conversation_id=instance.conversation_id,
            render_options=render_options,
            request_overrides=request_overrides,
            communication_stream_id=communication_stream_id,
        )
        return await self._run_agent_instance(task, instance, request)

    async def submit_agent_instance_message(
        self,
        *,
        agent_instance_id: str,
        user_text: str,
        render_options: dict[str, Any] | None = None,
        request_overrides: dict[str, Any] | None = None,
        communication_stream_id: str | None = None,
        client_message_id: str | None = None,
        attachments: tuple[PendingUserMessageAttachment, ...] = (),
    ) -> AgentMessageSubmissionResult:
        instance = self._runtime_gateway.get_agent_instance(agent_instance_id)
        task = self._runtime_gateway.get_task(instance.task_id)
        if task.is_terminal:
            if task.scene_id is None:
                raise ValueError(f"Agent 任务已结束，不能继续对话: {task.task_id}")
            task, instance = self._runtime_gateway.prepare_scene_root_agent_instance_for_new_task(
                instance.instance_id,
            )
        instance, pending_message = self._runtime_gateway.enqueue_agent_instance_user_message(
            instance.instance_id,
            user_text=user_text,
            attachments=attachments,
            client_message_id=client_message_id,
        )
        request = AgentExecutionRequest.from_text(
            thread_id=instance.thread_id,
            current_working_directory=instance.current_working_directory,
            agent_definition_id=instance.agent_definition_id,
            llm_provider_id=instance.llm_provider_id,
            llm_model_binding_id=instance.llm_model_binding_id,
            user_text=user_text,
            task_id=task.task_id,
            root_instance_id=instance.instance_id,
            conversation_id=instance.conversation_id,
            render_options=render_options,
            request_overrides=request_overrides,
            communication_stream_id=communication_stream_id,
            client_message_id=client_message_id,
        )
        communication_context = self._build_communication_context(request, task, instance)
        if request.communication_stream_id != communication_context.stream_id:
            request = replace(request, communication_stream_id=communication_context.stream_id)
        is_running = (
            instance.status == AGENT_INSTANCE_STATUS_RUNNING
            or instance.instance_id in self._active_instance_ids
        )
        if is_running:
            logger.info(
                "Agent 实例正在运行，新用户消息进入等待队列: instance_id=%s, task_id=%s, pending_message_id=%s, status=%s, active_in_memory=%s",
                instance.instance_id,
                task.task_id,
                pending_message.message_id,
                instance.status,
                instance.instance_id in self._active_instance_ids,
            )
            return AgentMessageSubmissionResult(
                status="queued",
                task_id=task.task_id,
                instance_id=instance.instance_id,
                conversation_id=instance.conversation_id,
                communication_stream_id=communication_context.stream_id,
                pending_message_id=pending_message.message_id,
                client_message_id=pending_message.client_message_id,
                queued_for_later=True,
                instance=instance,
            )
        logger.info(
            "Agent 实例启动后台执行: instance_id=%s, task_id=%s, pending_message_id=%s",
            instance.instance_id,
            task.task_id,
            pending_message.message_id,
        )
        self._active_instance_ids.add(instance.instance_id)
        task_handle = asyncio.create_task(self._run_agent_instance(task, instance, request))
        self._background_tasks.add(task_handle)
        task_handle.add_done_callback(
            lambda done_task, instance_id=instance.instance_id: self._on_background_task_done(done_task, instance_id)
        )
        return AgentMessageSubmissionResult(
            status="accepted",
            task_id=task.task_id,
            instance_id=instance.instance_id,
            conversation_id=instance.conversation_id,
            communication_stream_id=communication_context.stream_id,
            pending_message_id=pending_message.message_id,
            client_message_id=pending_message.client_message_id,
            queued_for_later=False,
            instance=instance,
        )

    def _on_background_task_done(self, task: asyncio.Task[AgentExecutionResult], instance_id: str) -> None:
        self._background_tasks.discard(task)
        self._active_instance_ids.discard(instance_id)
        with suppress(asyncio.CancelledError):
            exc = task.exception()
            if exc is not None:
                logger.error("Agent 后台执行失败。", exc_info=(type(exc), exc, exc.__traceback__))
                return
            logger.info("Agent 后台执行结束: instance_id=%s", instance_id)

    async def _run_agent_instance(
        self,
        task: AgentTask,
        instance: AgentInstance,
        request: AgentExecutionRequest,
    ) -> AgentExecutionResult:
        communication_context = self._build_communication_context(request, task, instance)
        trace_path = self._trace_recorder.start_trace(_trace_identity(instance))
        conversation: Any | None = None
        loop_index = 0
        logger.info(
            "Agent 执行开始: task_id=%s, instance_id=%s, conversation_id=%s, stream_id=%s",
            task.task_id,
            instance.instance_id,
            request.conversation_id or instance.conversation_id,
            communication_context.stream_id,
        )
        try:
            self._runtime_gateway.start_task(task.task_id)
            self._runtime_gateway.start_agent_instance(instance.instance_id)
            await self._publish_task_status(
                communication_context,
                "agent.task.started",
                task_id=task.task_id,
                instance_id=instance.instance_id,
            )
            conversation = await self._load_or_create_conversation(instance, request)
            communication_context = communication_context.with_metadata(conversation_id=conversation.id)

            plain_text_fallback_count = 0
            while True:
                loop_index += 1
                instance = self._runtime_gateway.get_agent_instance(instance.instance_id)
                logger.info(
                    "Agent 循环轮次开始: task_id=%s, instance_id=%s, loop_index=%s, status=%s, pending_user_messages=%s",
                    task.task_id,
                    instance.instance_id,
                    loop_index,
                    instance.status,
                    len(instance.pending_user_messages),
                )
                await self._drain_pending_user_messages_to_conversation(
                    instance_id=instance.instance_id,
                    conversation=conversation,
                    communication_context=communication_context,
                    trace_path=trace_path,
                )
                instance = self._runtime_gateway.get_agent_instance(instance.instance_id)
                resolved_candidates = self._resolve_instance_llm_candidates(instance)
                agent_definition = self._load_agent_definition(instance.agent_definition_id)
                llm_response = await _await_with_progress_logging(
                    self._send_prompt_with_policy_failover(
                        conversation=conversation,
                        agent_definition=agent_definition,
                        instance=instance,
                        resolved_candidates=resolved_candidates,
                        render_options=request.render_options,
                        request_overrides=request.request_overrides,
                        communication_context=communication_context,
                    ),
                    stage="llm_prompt",
                    task_id=task.task_id,
                    instance_id=instance.instance_id,
                    metadata={
                        "loop_index": loop_index,
                        "conversation_id": conversation.id,
                        "candidate_count": len(resolved_candidates),
                    },
                )
                assistant_message = llm_response.assistant_message
                _record_last_context_usage(conversation, llm_response)
                await self._conversation_repository.save(conversation)
                self._trace_recorder.record_llm_response(trace_path, assistant_message)
                await self._event_publisher.publish(
                    communication_context,
                    "agent.assistant.message.received",
                    {
                        "part_count": len(assistant_message.content),
                        "tool_use_count": len(_extract_tool_uses(assistant_message)),
                    },
                    source="agent_execution_engine",
                )

                tool_uses = _extract_tool_uses(assistant_message)
                if not tool_uses:
                    plain_text_fallback_count += 1
                    self._trace_recorder.record_event(
                        trace_path,
                        "plain_text_fallback",
                        {
                            "fallback_count": plain_text_fallback_count,
                            "threshold": _PLAIN_TEXT_FALLBACK_PAUSE_THRESHOLD,
                        },
                    )
                    if plain_text_fallback_count >= _PLAIN_TEXT_FALLBACK_PAUSE_THRESHOLD:
                        self._runtime_gateway.pause_agent_instance(instance.instance_id)
                        self._runtime_gateway.pause_task(task.task_id)
                        self._trace_recorder.record_event(
                            trace_path,
                            "instance_paused",
                            "普通文本 fallback 连续达到阈值，Runtime 主动暂停任务。",
                        )
                        await self._event_publisher.publish(
                            communication_context,
                            "agent.task.paused",
                            {
                                "task_id": task.task_id,
                                "agent_instance_id": instance.instance_id,
                                "reason": "plain_text_fallback_threshold_reached",
                                "fallback_count": plain_text_fallback_count,
                            },
                            source="agent_execution_engine",
                        )
                        result = AgentExecutionResult(
                            task_id=task.task_id,
                            instance_id=instance.instance_id,
                            conversation_id=conversation.id,
                            trace_file_path=str(trace_path),
                            assistant_message=assistant_message,
                            communication_stream_id=communication_context.stream_id,
                        )
                        next_execution = await self._resume_from_pending_user_messages(
                            task=task,
                            instance=instance,
                            request=request,
                            conversation=conversation,
                            communication_context=communication_context,
                            trace_path=trace_path,
                        )
                        if next_execution is None:
                            return result
                        task, instance, request, conversation, communication_context, trace_path = next_execution
                        plain_text_fallback_count = 0
                        continue
                    continue
                plain_text_fallback_count = 0

                tool_result_message, tool_results = await _await_with_progress_logging(
                    self._execute_tool_uses(
                        agent_definition,
                        tool_uses,
                        communication_context,
                    ),
                    stage="tool_batch",
                    task_id=task.task_id,
                    instance_id=instance.instance_id,
                    metadata={
                        "loop_index": loop_index,
                        "tool_count": len(tool_uses),
                        "tool_names": [tool_use.name for tool_use in tool_uses],
                    },
                )
                conversation.append_message(tool_result_message)
                await self._conversation_repository.save(conversation)
                self._trace_recorder.record_tool_result(trace_path, tool_result_message)
                loop_decision = _select_loop_decision(tool_results)
                logger.info(
                    "Agent 循环工具结果已落库: task_id=%s, instance_id=%s, loop_index=%s, tool_count=%s, loop_decision=%s",
                    task.task_id,
                    instance.instance_id,
                    loop_index,
                    len(tool_results),
                    loop_decision,
                )
                if loop_decision != "continue":
                    await self._apply_loop_decision(
                        loop_decision=loop_decision,
                        task_id=task.task_id,
                        instance_id=instance.instance_id,
                        trace_path=trace_path,
                        tool_results=tool_results,
                        communication_context=communication_context,
                    )
                    result = AgentExecutionResult(
                        task_id=task.task_id,
                        instance_id=instance.instance_id,
                        conversation_id=conversation.id,
                        trace_file_path=str(trace_path),
                        assistant_message=assistant_message,
                        communication_stream_id=communication_context.stream_id,
                    )
                    next_execution = await self._resume_from_pending_user_messages(
                        task=task,
                        instance=instance,
                        request=request,
                        conversation=conversation,
                        communication_context=communication_context,
                        trace_path=trace_path,
                    )
                    if next_execution is None:
                        return result
                    task, instance, request, conversation, communication_context, trace_path = next_execution
                    plain_text_fallback_count = 0
                    continue

            raise RuntimeError("Agent 执行循环异常结束。")
        except Exception as exc:
            failure_message = str(exc)
            logger.exception(
                "Agent 执行异常中断: task_id=%s, instance_id=%s, error_type=%s, error_message=%s",
                task.task_id,
                instance.instance_id,
                exc.__class__.__name__,
                failure_message,
            )
            self._runtime_gateway.interrupt_agent_instance(instance.instance_id, failure_message)
            self._trace_recorder.record_failure(trace_path, failure_message)
            await self._event_publisher.publish(
                communication_context,
                "agent.execution.interrupted",
                {
                    "task_id": task.task_id,
                    "agent_instance_id": instance.instance_id,
                    "error_type": exc.__class__.__name__,
                    "error_message": failure_message,
                    "resumable": True,
                    "retryable": True,
                },
                source="agent_execution_engine",
            )
            current_instance = self._runtime_gateway.get_agent_instance(instance.instance_id)
            if conversation is not None and current_instance.pending_user_messages:
                current_task = self._runtime_gateway.get_task(current_instance.task_id)
                next_request = replace(
                    request,
                    thread_id=current_instance.thread_id,
                    current_working_directory=current_instance.current_working_directory,
                    agent_definition_id=current_instance.agent_definition_id,
                    llm_provider_id=current_instance.llm_provider_id,
                    llm_model_binding_id=current_instance.llm_model_binding_id,
                    task_id=current_task.task_id,
                    root_instance_id=current_instance.instance_id,
                    conversation_id=current_instance.conversation_id,
                    communication_stream_id=communication_context.stream_id,
                )
                return await self._run_agent_instance(current_task, current_instance, next_request)
            return AgentExecutionResult(
                task_id=task.task_id,
                instance_id=instance.instance_id,
                conversation_id=(
                    conversation.id
                    if conversation is not None
                    else (request.conversation_id or instance.conversation_id or "")
                ),
                trace_file_path=str(trace_path),
                assistant_message=None,
                communication_stream_id=communication_context.stream_id,
            )

    async def _resume_from_pending_user_messages(
        self,
        *,
        task: AgentTask,
        instance: AgentInstance,
        request: AgentExecutionRequest,
        conversation: Any,
        communication_context: RuntimeCommunicationContext,
        trace_path: Any,
    ) -> tuple[AgentTask, AgentInstance, AgentExecutionRequest, Any, RuntimeCommunicationContext, Any] | None:
        """一次完整执行轮次结束后，检查待处理消息队列并按需触发后续对话。

        当前轮次的 loop_decision 或 interruption 语义必须先完整落地；之后如果 AgentInstance
        队列中还有用户消息，才根据当前 task 生命周期决定继续同 task，或 Scene root 创建新 task。
        """
        current_instance = self._runtime_gateway.get_agent_instance(instance.instance_id)
        if not current_instance.pending_user_messages:
            return None
        current_task = self._runtime_gateway.get_task(current_instance.task_id)
        if current_task.is_terminal:
            if current_task.scene_id is None:
                await self._event_publisher.publish(
                    communication_context,
                    "agent.pending_user_messages.blocked",
                    {
                        "task_id": current_task.task_id,
                        "agent_instance_id": current_instance.instance_id,
                        "pending_message_count": len(current_instance.pending_user_messages),
                        "reason": "non_scene_terminal_task",
                    },
                    source="agent_execution_engine",
                )
                return None
            current_task, current_instance = self._runtime_gateway.prepare_scene_root_agent_instance_for_new_task(
                current_instance.instance_id,
            )
        next_request = replace(
            request,
            thread_id=current_instance.thread_id,
            current_working_directory=current_instance.current_working_directory,
            agent_definition_id=current_instance.agent_definition_id,
            llm_provider_id=current_instance.llm_provider_id,
            llm_model_binding_id=current_instance.llm_model_binding_id,
            task_id=current_task.task_id,
            root_instance_id=current_instance.instance_id,
            conversation_id=current_instance.conversation_id,
            communication_stream_id=communication_context.stream_id,
        )
        next_context = self._build_communication_context(next_request, current_task, current_instance)
        if next_request.communication_stream_id != next_context.stream_id:
            next_request = replace(next_request, communication_stream_id=next_context.stream_id)
        next_trace_path = self._trace_recorder.start_trace(_trace_identity(current_instance))
        self._runtime_gateway.start_task(current_task.task_id)
        self._runtime_gateway.start_agent_instance(current_instance.instance_id)
        await self._publish_task_status(
            next_context,
            "agent.task.started",
            task_id=current_task.task_id,
            instance_id=current_instance.instance_id,
        )
        next_conversation = await self._load_or_create_conversation(current_instance, next_request)
        next_context = next_context.with_metadata(conversation_id=next_conversation.id)
        return current_task, current_instance, next_request, next_conversation, next_context, next_trace_path

    def _build_communication_context(
        self,
        request: AgentExecutionRequest,
        task: AgentTask,
        instance: AgentInstance,
    ) -> RuntimeCommunicationContext:
        """构建当前 Agent 执行链路透传的通信上下文。

        stream_id 选择优先级：
        1. request.communication_stream_id 显式指定时优先复用，保留调用方控制能力；
        2. task.scene_id 非空时默认使用 stream-scene-{scene_id}，同一 scene 下多个 task 共享同一通信流；
        3. 其他情况沿用 stream-agent-task-{task_id}，保证未挂场景的以往任务进程内以及升级后的 task.scene_id=null 考古代码路径仍然可用。
        """
        task_id = task.task_id
        stream_id = request.communication_stream_id
        if stream_id is None:
            stream_id = self._default_stream_id_for_task(task)
            if self._communication_gateway is not None:
                try:
                    stream_id = self._ensure_communication_stream(
                        stream_id=stream_id,
                        task=task,
                        instance=instance,
                    )
                except Exception:
                    logger.exception("创建 Agent 通信流失败，执行流程将继续: stream_id=%s", stream_id)
        elif self._communication_gateway is not None:
            try:
                stream_id = self._ensure_communication_stream(
                    stream_id=stream_id,
                    task=task,
                    instance=instance,
                )
            except Exception:
                logger.exception("确认 Agent 通信流失败，执行流程将继续: stream_id=%s", stream_id)
        return RuntimeCommunicationContext(
            stream_id=stream_id,
            actor_type="agent_instance",
            actor_id=instance.instance_id,
            trace_id=instance.trace_id,
            metadata={
                "task_id": task_id,
                "scene_id": task.scene_id,
                "thread_id": instance.thread_id,
                "agent_instance_id": instance.instance_id,
                "agent_definition_id": instance.agent_definition_id,
            },
        )

    @staticmethod
    def _default_stream_id_for_task(task: AgentTask) -> str:
        """根据 task 计算默认通信流 ID。

        scene_id 非空：stream-scene-{scene_id}，同一 scene 下跨 task 共享。
        scene_id 为 None：stream-agent-task-{task_id}，保留以往语义以适配未迁移的旧任务。
        """
        if task.scene_id:
            return f"stream-scene-{task.scene_id}"
        return f"stream-agent-task-{task.task_id}"

    def _ensure_communication_stream(
        self,
        *,
        stream_id: str,
        task: AgentTask,
        instance: AgentInstance,
    ) -> str:
        """确保当前 Agent 执行关联的逻辑通信流存在；不存在则创建。"""
        if self._communication_gateway is None:
            return stream_id
        try:
            stream = self._communication_gateway.get_stream(stream_id)
        except KeyError:
            try:
                topic = "agent.scene" if task.scene_id else "agent.task"
                stream = self._communication_gateway.create_stream(
                    CreateCommunicationStreamRequest(
                        stream_id=stream_id,
                        topic=topic,
                        owner_id=instance.agent_definition_id,
                        metadata={
                            "task_id": task.task_id,
                            "scene_id": task.scene_id,
                            "thread_id": instance.thread_id,
                            "agent_instance_id": instance.instance_id,
                            "agent_definition_id": instance.agent_definition_id,
                            "trace_id": instance.trace_id,
                        },
                    )
                )
            except ValueError:
                stream = self._communication_gateway.get_stream(stream_id)
        return stream.stream_id

    async def _publish_task_status(
        self,
        communication_context: RuntimeCommunicationContext,
        event_type: str,
        *,
        task_id: str,
        instance_id: str,
    ) -> None:
        """发布任务和根 Agent 实例状态事件。"""
        await self._event_publisher.publish(
            communication_context,
            event_type,
            {
                "task_id": task_id,
                "agent_instance_id": instance_id,
            },
            source="agent_execution_engine",
        )

    def _load_agent_definition(self, agent_definition_id: str) -> AgentDefinition:
        """加载并校验 Agent 静态定义。"""
        definition = self._agent_definition_gateway.get_agent_definition(agent_definition_id)
        if definition.disabled:
            raise ValueError(f"Agent 定义已禁用，不能执行: {agent_definition_id}")
        return definition

    def _resolve_instance_llm_candidates(
        self,
        instance: AgentInstance,
    ) -> tuple[ResolvedLlmProviderModel, ...]:
        candidates = instance.llm_policy_snapshot_json.get("candidates")
        if isinstance(candidates, list) and candidates:
            resolved: list[ResolvedLlmProviderModel] = []
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                provider_id = candidate.get("provider_id")
                model_binding_id = candidate.get("model_binding_id")
                if not provider_id or not model_binding_id:
                    continue
                resolved.append(
                    self._llm_registry_gateway.resolve_provider_model(
                        str(provider_id),
                        str(model_binding_id),
                    )
                )
            if resolved:
                return tuple(resolved)
        return (
            self._llm_registry_gateway.resolve_provider_model(
                instance.llm_provider_id,
                instance.llm_model_binding_id,
            ),
        )

    async def _send_prompt_with_policy_failover(
        self,
        *,
        conversation: Any,
        agent_definition: AgentDefinition,
        instance: AgentInstance,
        resolved_candidates: tuple[ResolvedLlmProviderModel, ...],
        render_options: dict[str, Any],
        request_overrides: dict[str, Any],
        communication_context: RuntimeCommunicationContext,
    ) -> LlmPromptResponse:
        last_error: Exception | None = None
        skipped_for_modalities = False
        unsupported_modality_strategy = _unsupported_input_modality_strategy(instance)
        llm_call_id = f"llm-call-{uuid.uuid4().hex}"
        for candidate_index, resolved_model in enumerate(resolved_candidates):
            missing_modalities = _missing_input_modalities(conversation, resolved_model)
            if (
                missing_modalities
                and unsupported_modality_strategy == UNSUPPORTED_INPUT_MODALITY_STRATEGY_SKIP_CANDIDATE
            ):
                await self._event_publisher.publish(
                    communication_context,
                    "llm.policy.candidate.skipped",
                    {
                        "policy_group_id": instance.llm_policy_group_id,
                        "candidate_index": candidate_index,
                        "provider_id": resolved_model.provider_id,
                        "binding_id": resolved_model.binding_id,
                        "model_id": resolved_model.model_id,
                        "reason": "unsupported_input_modalities",
                        "conversation_modalities": list(getattr(conversation.state, "modalities", [])),
                        "supported_input_modalities": list(resolved_model.supported_input_modalities),
                        "missing_input_modalities": missing_modalities,
                    },
                    source="agent_execution_engine",
                )
                skipped_for_modalities = True
                continue
            system_prompt = self._render_system_prompt(
                agent_definition,
                instance.current_working_directory,
                render_options,
            )
            tools = await self._tool_dispatcher.build_llm_tools(
                agent_definition=agent_definition,
                api_format=resolved_model.api_format,
            )
            try:
                return await self._llm_request_service.send_prompt(
                    LlmPromptRequest(
                        conversation=conversation,
                        resolved_model=resolved_model,
                        system_prompt=system_prompt,
                        tools=tools,
                        render_options=render_options,
                        request_overrides=request_overrides,
                        communication_context=communication_context.with_actor(
                            actor_type="llm_client",
                            actor_id=instance.instance_id,
                        ),
                        llm_call_id=llm_call_id,
                        candidate_index=candidate_index,
                        next_resolved_model=resolved_candidates[candidate_index + 1]
                        if candidate_index < len(resolved_candidates) - 1
                        else None,
                    )
                )
            except Exception as exc:
                last_error = exc
                can_failover = (
                    candidate_index < len(resolved_candidates) - 1
                    and isinstance(exc, LlmProviderModelServiceUnavailable)
                )
                await self._event_publisher.publish(
                    communication_context,
                    "llm.policy.candidate.failed",
                    {
                        "policy_group_id": instance.llm_policy_group_id,
                        "candidate_index": candidate_index,
                        "provider_id": resolved_model.provider_id,
                        "binding_id": resolved_model.binding_id,
                        "model_id": resolved_model.model_id,
                        "error_type": exc.__class__.__name__,
                        "error_message": str(exc),
                        "will_failover": can_failover,
                    },
                    source="agent_execution_engine",
                )
                if not can_failover:
                    raise
                next_model = resolved_candidates[candidate_index + 1]
                await self._event_publisher.publish(
                    communication_context,
                    "llm.policy.failover",
                    {
                        "policy_group_id": instance.llm_policy_group_id,
                        "from_provider_id": resolved_model.provider_id,
                        "from_binding_id": resolved_model.binding_id,
                        "to_provider_id": next_model.provider_id,
                        "to_binding_id": next_model.binding_id,
                        "reason": exc.__class__.__name__,
                    },
                    source="agent_execution_engine",
                )
        if last_error is not None:
            raise last_error
        if skipped_for_modalities:
            raise RuntimeError("LLM 执行策略组没有支持当前会话输入模态的可用候选项。")
        raise RuntimeError("LLM 执行策略组没有可用候选项。")

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

    async def _drain_pending_user_messages_to_conversation(
        self,
        *,
        instance_id: str,
        conversation: Any,
        communication_context: RuntimeCommunicationContext,
        trace_path: Any,
    ) -> int:
        """在完整执行轮次边界消费 AgentInstance 待处理用户消息队列。

        用户消息无论是在 ready 还是 running 时提交，都会先进入 AgentInstance 队列；
        执行循环只在下一次 LLM 请求前按 FIFO 顺序追加到 conversation 末尾。
        """
        _updated_instance, pending_messages = self._runtime_gateway.drain_agent_instance_user_messages(
            instance_id,
        )
        if not pending_messages:
            return 0
        for pending_message in pending_messages:
            parts, parts_meta = _pending_user_message_parts(pending_message)
            conversation.add_user_message(
                parts,
                parts_meta,
            )
            user_message = conversation.messages[-1]
            self._trace_recorder.record_user_message(trace_path, user_message)
            await self._event_publisher.publish(
                communication_context,
                "user.message.accepted",
                {
                    "pending_message_id": pending_message.message_id,
                    "client_message_id": pending_message.client_message_id,
                    "user_message_id": f"{conversation.id}:message:{len(conversation.messages) - 1}",
                    "text": _message_text(user_message),
                    "attachments": _pending_user_message_attachments_payload(pending_message),
                    "part_count": len(user_message.content),
                    "queued_received_at": pending_message.received_at.isoformat(),
                },
                source="agent_execution_engine",
            )
        await self._conversation_repository.save(conversation)
        return len(pending_messages)

    def _render_system_prompt(
        self,
        definition: AgentDefinition,
        current_working_directory: str,
        render_options: dict[str, Any],
    ) -> str | None:
        """按 AgentDefinition.system_prompt 顺序拼接最终系统提示词。"""
        segments: list[str] = []
        for segment in definition.system_prompt:
            if segment.text is not None:
                segments.append(segment.text)
            elif segment.template_id is not None:
                snippet = self._prompt_snippet_gateway.get_snippet(segment.template_id)
                segments.append(snippet.text)
        text = "\n\n".join(item for item in segments if item.strip())
        runtime_context_prompt = _build_runtime_context_prompt(current_working_directory)
        mcp_services_prompt = self._build_mcp_services_prompt(definition)
        skills_prompt = self._build_skills_prompt(definition)
        lifecycle_prompt = _build_lifecycle_runtime_prompt(render_options)
        lifecycle_tool_notice = self._build_lifecycle_tool_notice(definition)
        return "\n\n".join(
            item
            for item in (
                text,
                runtime_context_prompt,
                mcp_services_prompt,
                skills_prompt,
                lifecycle_prompt,
                lifecycle_tool_notice,
            )
            if item.strip()
        ) or None

    def _build_mcp_services_prompt(self, definition: AgentDefinition) -> str:
        if not definition.mcp_tools or self._mcp_client_gateway is None:
            return ""
        service_ids = []
        for binding in definition.mcp_tools:
            if binding.service_id not in service_ids:
                service_ids.append(binding.service_id)
        services = {
            item.service_id: item
            for item in self._mcp_client_gateway.list_services()
        }
        available_services = [services[sid] for sid in service_ids if sid in services]
        if not available_services:
            return ""
        lines = ["# 可用的 MCP 服务列表"]
        for service in available_services:
            description = service.description or ""
            lines.append(f"- `{service.service_id}`：`{description}`")
        lines.append("## 使用方式")
        lines.append(
            "用 MCP 服务相关工具读取服务详情和 MCP tool 详情，了解 MCP 服务的功能和使用方式，"
            "然后使用 MCP tool 调用工具调用对应 MCP tool 并获取执行结果。"
        )
        return "\n".join(lines)

    def _build_skills_prompt(self, definition: AgentDefinition) -> str:
        if not definition.skill_ids or self._skill_gateway is None:
            return ""
        skills = {
            item.skill_id: item
            for item in self._skill_gateway.list_skills()
        }
        available_skills = [skills[sid] for sid in definition.skill_ids if sid in skills]
        if not available_skills:
            return ""
        lines = ["# 你可用的 Skills"]
        for skill in available_skills:
            lines.append(
                f"- `{skill.skill_id}`：skill 触发场景=`{skill.description}`；"
                f"该 skill 仓库目录=`{skill.directory_path}`"
            )
        lines.append("## skill 使用技巧")
        lines.append(
            "Skill 是 Agent 的“某个领域专家经验”，对你处理该领域的问题有极大的帮助。"
            "每个 skill 都有他们适用的场景/领域，根据你当前的和接下来的目标来决定是否要读取某个 skill 的详细内容。"
            "skill 仓库都是标准规范的 skill 目录，当你需要某个 skill 时，"
            "使用命令行或是目录结构读取工具获取该 skill 仓库目录结构，"
            "然后首先读取他的 SKILL.md 文件，再决定是否读取其他资源文件。"
        )
        return "\n".join(lines)

    def _build_lifecycle_tool_notice(self, definition: AgentDefinition) -> str:
        tools = self._local_tool_gateway.list_tools(public_only=False, group_by=False)
        lifecycle_tool_names = [
            item.tool_name
            for item in tools
            if item.category == "lifecycle"
            and (item.always_enabled or item.tool_name in definition.tool_ids)
        ]
        if not lifecycle_tool_names:
            return ""
        names = "，".join(lifecycle_tool_names)
        return (
            "# 工具调用注意事项\n"
            f"- 禁止并发调用“{names}”这些工具，这些工具只能单独调用！这些工具只能单独调用！这些工具只能单独调用！"
        )

    async def _execute_single_tool_use(
        self,
        definition: AgentDefinition,
        tool_use: ToolUsePart,
        communication_context: RuntimeCommunicationContext,
    ) -> ToolDispatchResult:
        """执行单个工具调用，调度层异常也转换为 LLM 可见的工具错误结果。"""
        try:
            return await self._tool_dispatcher.dispatch_tool_call(
                agent_definition=definition,
                tool_use=tool_use,
                communication_context=communication_context.with_actor(
                    actor_type="tool_dispatcher",
                    actor_id=tool_use.name,
                ),
            )
        except Exception as exc:
            tool_call_id = _tool_use_ref(tool_use)
            tool_result = build_tool_error_result(
                error_code=exc.__class__.__name__,
                error_message=str(exc),
            )
            return ToolDispatchResult(
                tool_name=tool_use.name,
                source="tool_dispatcher",
                result_part=ToolResultPart(
                    tool_use_ref=tool_call_id,
                    is_error=True,
                    output=tool_result.error_message or "Tool dispatch failed.",
                ),
                tool_result=tool_result,
            )

    async def _execute_tool_uses(
        self,
        definition: AgentDefinition,
        tool_uses: list[ToolUsePart],
        communication_context: RuntimeCommunicationContext,
    ) -> tuple[UniMessage, list[BaseToolResult]]:
        """执行 assistant 消息中的全部工具调用并生成 tool 消息。"""
        started_at = time.perf_counter()
        logger.info(
            "Agent 工具批执行开始: tool_count=%s, tool_names=%s",
            len(tool_uses),
            [tool_use.name for tool_use in tool_uses],
        )
        result_parts = []
        tasks = [
            asyncio.create_task(self._execute_single_tool_use(
                definition,
                tool_use,
                communication_context,
            ))
            for tool_use in tool_uses
        ]
        results = await asyncio.gather(*tasks)
        tool_results = []
        for result in results:
            result_parts.append(result.result_part)
            tool_results.append(result.tool_result)
            await self._publish_tool_frontend_message(result.tool_result, result.result_part.tool_use_ref, communication_context)
        logger.info(
            "Agent 工具批执行完成: tool_count=%s, loop_decisions=%s, 耗时=%sms",
            len(tool_results),
            [result.loop_decision for result in tool_results],
            int((time.perf_counter() - started_at) * 1000),
        )
        return UniMessage(role=ROLE_TOOL, content=result_parts), tool_results

    async def _publish_tool_frontend_message(
        self,
        tool_result: BaseToolResult,
        tool_call_id: str,
        communication_context: RuntimeCommunicationContext,
    ) -> None:
        if tool_result.frontend_message is None:
            return
        action = tool_result.metadata.get("lifecycle_action")
        if action == "request_response":
            message = tool_result.frontend_message
            await self._event_publisher.publish(
                communication_context,
                "agent.interaction.requested",
                {
                    "interaction_id": tool_call_id,
                    "tool_call_id": tool_call_id,
                    "question": {
                        "text": message.text,
                        "text_format": message.text_format,
                    },
                    "options": message.metadata.get("options", []),
                    "allow_free_text": True,
                    "loop_control": tool_result.loop_control.model_dump(mode="json"),
                },
                source="agent_execution_engine",
                correlation_id=tool_call_id,
            )
            return
        await self._event_publisher.publish(
            communication_context,
            "agent.lifecycle.message",
            {
                "tool_call_id": tool_call_id,
                "lifecycle_action": action,
                "frontend_message": tool_result.frontend_message.model_dump(mode="json"),
                "loop_control": tool_result.loop_control.model_dump(mode="json"),
            },
            source="agent_execution_engine",
            correlation_id=tool_call_id,
        )

    async def _apply_loop_decision(
        self,
        *,
        loop_decision: LoopDecision,
        task_id: str,
        instance_id: str,
        trace_path: Any,
        tool_results: list[BaseToolResult],
        communication_context: RuntimeCommunicationContext,
    ) -> None:
        """根据统一工具结果中的 loop_decision 更新运行态。"""
        details = [result.to_serializable_dict() for result in tool_results]
        if loop_decision == "pause":
            self._runtime_gateway.pause_agent_instance(instance_id)
            self._runtime_gateway.pause_task(task_id)
            self._trace_recorder.record_event(trace_path, "instance_paused", details)
            await self._publish_loop_decision_event(
                communication_context,
                "agent.task.paused",
                task_id=task_id,
                instance_id=instance_id,
                loop_decision=loop_decision,
                tool_results=tool_results,
            )
            return
        if loop_decision == "complete":
            task = self._runtime_gateway.get_task(task_id)
            if task.scene_id is not None:
                self._runtime_gateway.complete_scene_task_and_reset_root_agent_instance(
                    task_id,
                    instance_id,
                )
            else:
                self._runtime_gateway.complete_agent_instance(instance_id)
                self._runtime_gateway.complete_task(task_id)
            self._trace_recorder.record_completed(trace_path)
            await self._publish_loop_decision_event(
                communication_context,
                "agent.task.completed",
                task_id=task_id,
                instance_id=instance_id,
                loop_decision=loop_decision,
                tool_results=tool_results,
            )
            return
        if loop_decision == "fail":
            failure_message = _failure_message_from_tool_results(tool_results)
            self._runtime_gateway.fail_agent_instance(instance_id, failure_message)
            self._runtime_gateway.fail_task(task_id, failure_message)
            self._trace_recorder.record_failure(trace_path, failure_message)
            await self._publish_loop_decision_event(
                communication_context,
                "agent.task.failed",
                task_id=task_id,
                instance_id=instance_id,
                loop_decision=loop_decision,
                tool_results=tool_results,
                failure_message=failure_message,
            )
            return
        raise ValueError(f"未知循环决策: {loop_decision}")

    async def _publish_loop_decision_event(
        self,
        communication_context: RuntimeCommunicationContext,
        event_type: str,
        *,
        task_id: str,
        instance_id: str,
        loop_decision: LoopDecision,
        tool_results: list[BaseToolResult],
        failure_message: str | None = None,
    ) -> None:
        """发布工具结果驱动的 Agent 循环决策事件。"""
        frontend_messages = [
            result.frontend_message.model_dump(mode="json")
            for result in tool_results
            if result.frontend_message is not None
        ]
        await self._event_publisher.publish(
            communication_context,
            event_type,
            {
                "task_id": task_id,
                "agent_instance_id": instance_id,
                "loop_decision": loop_decision,
                "tool_result_count": len(tool_results),
                "frontend_messages": frontend_messages,
                "failure_message": failure_message,
            },
            source="agent_execution_engine",
        )


def _request_user_text(request: AgentExecutionRequest) -> str:
    """从执行请求中提取可入队的用户文本。

    当前 Agent 实例待处理消息队列承载文本消息；REST 消息入口也是文本。
    如果历史调用方构造了多段文本，则按原顺序用空行拼接，保持用户输入语义。
    """
    texts = [
        part.text
        for part in request.user_parts
        if isinstance(part, TextPart) and part.text
    ]
    return "\n\n".join(texts)


def _request_user_attachments(
    request: AgentExecutionRequest,
) -> tuple[PendingUserMessageAttachment, ...]:
    attachments: list[PendingUserMessageAttachment] = []
    for index, part in enumerate(request.user_parts):
        part_meta = request.user_parts_meta[index] if index < len(request.user_parts_meta) else None
        if isinstance(part, ImagePart):
            attachments.append(
                PendingUserMessageAttachment(
                    kind=INPUT_MODALITY_IMAGE,
                    resource_id=part.resource_id,
                )
            )
        elif isinstance(part, PdfPart):
            attachments.append(
                PendingUserMessageAttachment(
                    kind=INPUT_MODALITY_PDF,
                    resource_id=part.resource_id,
                    page_count=part_meta.page_count if isinstance(part_meta, PdfPartMeta) else None,
                )
            )
    return tuple(attachments)


def _pending_user_message_parts(
    pending_message: PendingAgentUserMessage,
) -> tuple[list[UniPart], list[PartMeta]]:
    parts: list[UniPart] = []
    parts_meta: list[PartMeta] = []
    if pending_message.user_text.strip():
        parts.append(TextPart(text=pending_message.user_text))
        parts_meta.append(TextPartMeta())
    for attachment in pending_message.attachments:
        if attachment.kind == INPUT_MODALITY_IMAGE:
            parts.append(ImagePart(resource_id=attachment.resource_id))
            parts_meta.append(ImagePartMeta())
        elif attachment.kind == INPUT_MODALITY_PDF:
            parts.append(PdfPart(resource_id=attachment.resource_id))
            parts_meta.append(PdfPartMeta(page_count=attachment.page_count or 0))
    return parts, parts_meta


def _pending_user_message_attachments_payload(
    pending_message: PendingAgentUserMessage,
) -> list[dict[str, object | None]]:
    return [
        {
            "kind": attachment.kind,
            "resource_id": attachment.resource_id,
            "name": attachment.name,
            "mime_type": attachment.mime_type,
            "size_in_bytes": attachment.size_in_bytes,
            "width": attachment.width,
            "height": attachment.height,
            "page_count": attachment.page_count,
        }
        for attachment in pending_message.attachments
    ]


def _record_last_context_usage(conversation: Any, llm_response: LlmPromptResponse) -> None:
    token_usage = dict(llm_response.token_usage or {})
    conversation.state.last_context_usage = {
        "token_usage": token_usage,
        "context_window_used_tokens": int(token_usage.get("total_tokens") or 0),
        "context_window_max": llm_response.context_window_max,
        "provider_id": llm_response.provider_id,
        "binding_id": llm_response.binding_id,
        "model_id": llm_response.model_id,
        "model_alias": llm_response.model_alias,
    }


def _extract_tool_uses(message: UniMessage) -> list[ToolUsePart]:
    """从 assistant 消息中提取工具调用。"""
    return [part for part in message.content if isinstance(part, ToolUsePart)]


def _tool_use_ref(tool_use: ToolUsePart) -> str:
    """确定工具结果要关联的工具调用引用。"""
    return tool_use.tool_use_id or tool_use.name


def _unsupported_input_modality_strategy(instance: AgentInstance) -> str:
    strategy = instance.llm_policy_snapshot_json.get("unsupported_input_modality_strategy")
    if not strategy:
        return UNSUPPORTED_INPUT_MODALITY_STRATEGY_DEGRADE
    strategy_text = str(strategy)
    if strategy_text == UNSUPPORTED_INPUT_MODALITY_STRATEGY_SKIP_CANDIDATE:
        return strategy_text
    return UNSUPPORTED_INPUT_MODALITY_STRATEGY_DEGRADE


def _missing_input_modalities(
    conversation: Any,
    resolved_model: ResolvedLlmProviderModel,
) -> list[str]:
    conversation_modalities = getattr(getattr(conversation, "state", None), "modalities", [])
    supported_modalities = set(resolved_model.supported_input_modalities)
    return sorted(
        str(modality)
        for modality in conversation_modalities
        if str(modality) not in supported_modalities
    )


def _message_text(message: UniMessage) -> str:
    texts = [
        part.text
        for part in message.content
        if isinstance(part, TextPart) and part.text
    ]
    return "\n\n".join(texts)


def _build_runtime_context_prompt(current_working_directory: str) -> str:
    working_directory = Path(current_working_directory).expanduser()
    resolved_working_directory = working_directory.resolve(strict=False)
    lines = [
        "# Runtime 执行环境",
        f"- Current working directory: `{resolved_working_directory}`",
        f"- Operating system: `{platform.system()}`",
        f"- OS release: `{platform.release()}`",
        f"- OS version: `{platform.version()}`",
        f"- Machine: `{platform.machine()}`",
        f"- Python platform: `{platform.platform()}`",
    ]
    git_roots = _discover_git_roots_under(resolved_working_directory)
    if git_roots:
        lines.append("")
        lines.append("# 当前工作目录下的 Git 仓库根目录")
        for git_root in git_roots:
            lines.append(f"- `{git_root}`")
    return "\n".join(lines)


def _discover_git_roots_under(root: Path) -> list[str]:
    if not root.exists() or not root.is_dir():
        return []
    discovered: list[str] = []
    for current_root, dir_names, _file_names in os.walk(root):
        current_path = Path(current_root)
        if ".git" in dir_names:
            discovered.append(str(current_path))
            dir_names.remove(".git")
        excluded = {
            ".hg",
            ".svn",
            "__pycache__",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            "node_modules",
            ".venv",
            "venv",
        }
        dir_names[:] = [
            name
            for name in dir_names
            if name not in excluded
        ]
    return sorted(discovered)


def _build_lifecycle_runtime_prompt(render_options: dict[str, Any]) -> str:
    channel = str(
        render_options.get("channel")
        or render_options.get("frontend_channel")
        or "unknown"
    )
    supported_formats = _format_render_option_values(
        render_options.get("supported_text_formats")
        or render_options.get("text_formats")
        or ("markdown", "plain_text", "json")
    )
    preferred_format = str(
        render_options.get("preferred_text_format")
        or render_options.get("text_format")
        or "markdown"
    )
    return (
        "## Runtime lifecycle tool policy\n"
        "- Use lifecycle tools for all user-facing communication instead of plain assistant text.\n"
        "- `message_user` has two behavior modes via `action` field: "
        "`notify` (non-blocking, send-and-continue) for progress updates, important findings, "
        "or pre-announcing long-running operations; "
        "`request_response` (blocks until user replies) covering ALL scenarios that need user "
        "input — questions, confirmation, selection, reporting completion for review, "
        "dangerous operation approval, asking for help, etc. "
        "Do NOT use `notify` to ask questions or to wait for user reply.\n"
        "- `pause_task` tool: pause execution and wait for external events (other Agent messages "
        "or system events) — does NOT send a message. For waiting on user reply use "
        "`message_user(action=request_response)`, not `pause_task`.\n"
        "- `end_task` tool: end task session for SUCCESSFUL completion. Required sequence: "
        "first call `message_user(action=request_response)` to report results and wait for "
        "user verification; only after explicit user approval should you call `end_task`. "
        "For task failure use `fail_task` instead.\n"
        "- `fail_task` tool: declare unrecoverable failure and terminate the loop. Use only when "
        "you as the Agent judge that the task cannot proceed (missing required tool/permission, "
        "severely inconsistent input, repeated failures with no path forward). Routine tool "
        "exceptions are wrapped automatically by the framework — do not call `fail_task` for them. "
        "Recommended: first `message_user(action=notify)` to inform user of failure cause and "
        "attempted approaches, then call `fail_task`.\n"
        "- Do not call lifecycle tools in the same assistant turn as business tools unless the "
        "user explicitly requires it.\n"
        f"- Current frontend channel: `{channel}`.\n"
        f"- Supported text formats: `{supported_formats}`.\n"
        f"- Preferred text format: `{preferred_format}`.\n"
        "- Set `text_format` explicitly and do not return HTML.\n"
        "- `message_user` with `request_response`: `options` must be objects with `id`, `label`, "
        "and optional `description`."
    )


def _format_render_option_values(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(item) for item in value)
    return str(value)


def _trace_identity(instance: AgentInstance) -> AgentTraceIdentity:
    """从 AgentInstance 构造 Trace 文件身份信息。"""
    return AgentTraceIdentity(
        task_id=instance.task_id,
        instance_id=instance.instance_id,
        thread_id=instance.thread_id,
        agent_definition_id=instance.agent_definition_id,
        trace_id=instance.trace_id,
    )


def _select_loop_decision(tool_results: list[BaseToolResult]) -> LoopDecision:
    """从一组工具结果中选择本轮最终循环决策。"""
    priority: dict[LoopDecision, int] = {
        "continue": 0,
        "pause": 1,
        "complete": 2,
        "fail": 3,
    }
    selected: LoopDecision = "continue"
    for result in tool_results:
        decision = result.loop_decision
        if priority[decision] > priority[selected]:
            selected = decision
    return selected


def _failure_message_from_tool_results(tool_results: list[BaseToolResult]) -> str:
    """从工具结果中提取失败原因。"""
    for result in tool_results:
        if result.error_message:
            return result.error_message
        if result.loop_decision == "fail" and isinstance(result.output, str):
            return result.output
    return "工具返回 fail 循环决策。"


async def _await_with_progress_logging(
    awaitable: Awaitable[_T],
    *,
    stage: str,
    task_id: str,
    instance_id: str,
    metadata: dict[str, Any] | None = None,
) -> _T:
    started_at = time.perf_counter()
    task = asyncio.ensure_future(awaitable)
    metadata = dict(metadata or {})
    logger.info(
        "Agent 阶段开始: stage=%s, task_id=%s, instance_id=%s, metadata=%s",
        stage,
        task_id,
        instance_id,
        metadata,
    )
    try:
        while True:
            done, _pending = await asyncio.wait(
                {task},
                timeout=_AGENT_STAGE_PROGRESS_LOG_INTERVAL_SECONDS,
            )
            if done:
                result = task.result()
                logger.info(
                    "Agent 阶段完成: stage=%s, task_id=%s, instance_id=%s, 耗时=%sms, metadata=%s",
                    stage,
                    task_id,
                    instance_id,
                    int((time.perf_counter() - started_at) * 1000),
                    metadata,
                )
                return result
            logger.warning(
                "Agent 阶段仍在执行: stage=%s, task_id=%s, instance_id=%s, 已耗时=%sms, metadata=%s",
                stage,
                task_id,
                instance_id,
                int((time.perf_counter() - started_at) * 1000),
                metadata,
            )
    except asyncio.CancelledError:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        raise
    except Exception:
        logger.exception(
            "Agent 阶段失败: stage=%s, task_id=%s, instance_id=%s, 耗时=%sms, metadata=%s",
            stage,
            task_id,
            instance_id,
            int((time.perf_counter() - started_at) * 1000),
            metadata,
        )
        raise
