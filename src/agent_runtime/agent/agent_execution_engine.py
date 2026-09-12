# -*- coding: utf-8 -*-
"""Agent 执行循环引擎。"""
from __future__ import annotations

import asyncio
import os
import platform
import logging
import math
import time
import textwrap
import uuid
from copy import deepcopy
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

from agent_runtime.agent.agent_instance import (
    AGENT_INSTANCE_STATUS_READY,
    AGENT_INSTANCE_STATUS_RUNNING,
    AgentInstance,
    EXECUTION_SUBSTATE_NOTE_UPDATE,
    PendingAgentUserMessage,
    PendingUserMessageAttachment,
)
from agent_runtime.agent.agent_task import AgentTask
from agent_runtime.agent.agent_trace_recorder import AgentTraceIdentity, AgentTraceRecorder
from agent_runtime.agent.task_completion import (
    AgentTaskCompletionCoordinator,
    SceneTaskCompletionResult,
    TaskOperationGate,
)
from agent_runtime.agent.file_input_policy import validate_agent_instance_file_input_allowed
from agent_runtime.agent.execution_loop_logger import (
    AgentExecutionLoopLogger,
    NoopAgentExecutionLoopLogger,
)
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
from agent_runtime.prompt_template.prompt_variables import (
    PromptVariableContext,
    build_prompt_variable_values,
    render_prompt_variables,
)
from agent_runtime.conversation_message.conversation_repository import ConversationRepository
from agent_runtime.conversation_message.message.models import (
    Conversation,
    ConfigurationComponentEvent,
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
    ROLE_USER,
    ROLE_ASSISTANT,
    ROLE_TOOL,
)
from agent_runtime.conversation_message.message.tool_result_output import normalize_tool_result_output
from agent_runtime.conversation_message.message.operations import create_conversation
from agent_runtime.context_compression.compression_service import ContextCompressionService
from agent_runtime.context_compression.gate import decide_context_compression
from agent_runtime.context_compression.models import CompressionDecision
from agent_runtime.task_note.task_note_prompt_provider import (
    NOTE_UPDATE_SYSTEM_PROMPT_TEMPLATE_ID,
    TaskNotePromptProvider,
)
from agent_runtime.task_note.task_note_snapshot_service import (
    TaskNoteSnapshotService,
    get_task_note_snapshot_service,
)
from agent_runtime.infrastructure.runtime_task_registry import get_runtime_task_registry
from agent_runtime.input_modality import INPUT_MODALITY_IMAGE, INPUT_MODALITY_PDF
from agent_runtime.llm_client.llm_protocol_clients import LlmRequestCancelled
from agent_runtime.llm_client.llm_request_models import (
    LlmPromptRequest,
    LlmPromptResponse,
    LlmToolDefinition,
)
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
from agent_runtime.tool.local_tool_definition import is_tool_default_enabled_category
from agent_runtime.tool.north_gateway.local_tool_contracts import LocalToolGateway
from agent_runtime.tool.tool_execution_models import BaseToolResult, LoopDecision, build_tool_error_result


_PLAIN_TEXT_FALLBACK_PAUSE_THRESHOLD = 5
_AGENT_STAGE_PROGRESS_LOG_INTERVAL_SECONDS = 30.0
logger = logging.getLogger(__name__)
_T = TypeVar("_T")


def _safe_runtime_task_label(value: str | None, *, max_length: int = 80) -> str:
    raw = (value or "unknown").strip()
    safe = "".join(char if char.isalnum() or char in {".", "_", "-"} else "-" for char in raw)
    return (safe.strip("-._") or "unknown")[:max_length]


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
class AgentInterruptResult:
    status: str
    task_id: str
    instance_id: str
    conversation_id: str | None
    interrupt_requested: bool
    reason: str


@dataclass(slots=True)
class _AgentExecutionInterruptState:
    requested: bool = False
    reason: str = "user_requested"
    requested_at: float | None = None
    current_llm_request: dict[str, Any] | None = None
    current_llm_request_cancellers: set[Callable[[], None]] = field(default_factory=set)

    def register_current_llm_request_canceller(self, canceller: Callable[[], None]) -> Callable[[], None]:
        self.current_llm_request_cancellers.add(canceller)
        if self.requested:
            with suppress(Exception):
                canceller()
        return lambda: self.current_llm_request_cancellers.discard(canceller)

    def cancel_current_llm_requests(self) -> None:
        for canceller in tuple(self.current_llm_request_cancellers):
            with suppress(Exception):
                canceller()


@dataclass(frozen=True, slots=True)
class _AgentExecutionHandle:
    task: asyncio.Task[AgentExecutionResult]
    interrupt_state: _AgentExecutionInterruptState


@dataclass(frozen=True, slots=True)
class _PauseTimeoutHandle:
    token: str
    task: asyncio.Task[None]
    task_id: str
    instance_id: str
    conversation_id: str | None
    deadline: datetime
    timeout_seconds: float
    communication_stream_id: str | None
    request: AgentExecutionRequest


@dataclass(slots=True)
class _AgentExecutionProgressState:
    task_id: str
    instance_id: str
    stream_id: str | None
    conversation_id: str | None
    phase: str
    loop_index: int | None
    phase_started_at: float
    last_progress_at: float
    metadata: dict[str, Any] = field(default_factory=dict)
    running_tools: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _PromptWithConversationResult:
    response: LlmPromptResponse
    conversation: Any
    instance: AgentInstance
    conversation_history_copy_message_count: int


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

    message_metadata: dict[str, Any] = field(default_factory=dict)


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
        context_compression_service: ContextCompressionService | None = None,
        execution_loop_logger: AgentExecutionLoopLogger | NoopAgentExecutionLoopLogger | None = None,
        file_resource_service_getter: Callable[[], Any] | None = None,
        task_note_prompt_provider: TaskNotePromptProvider | None = None,
        task_note_snapshot_service: TaskNoteSnapshotService | None = None,
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
        self._context_compression_service = context_compression_service
        self._execution_loop_logger = execution_loop_logger or NoopAgentExecutionLoopLogger()
        self._file_resource_service_getter = file_resource_service_getter
        self._task_note_prompt_provider = task_note_prompt_provider or TaskNotePromptProvider(
            prompt_snippet_gateway=prompt_snippet_gateway,
            local_tool_gateway=local_tool_gateway,
        )
        self._task_note_snapshot_service = task_note_snapshot_service or get_task_note_snapshot_service()
        self._background_tasks: set[asyncio.Task[AgentExecutionResult]] = set()
        self._active_instance_ids: set[str] = set()
        self._active_execution_handles: dict[str, _AgentExecutionHandle] = {}
        self._pause_timeout_handles: dict[str, _PauseTimeoutHandle] = {}
        self._execution_progress: dict[str, _AgentExecutionProgressState] = {}
        self._task_operation_gate = TaskOperationGate()
        self._task_completion_coordinator = AgentTaskCompletionCoordinator(
            runtime_gateway=self._runtime_gateway,
            trace_recorder=self._trace_recorder,
            event_publisher=self._event_publisher,
            operation_gate=self._task_operation_gate,
            delete_task_terminals=self._delete_task_terminals,
            is_instance_active=lambda instance_id: (
                instance_id in self._active_instance_ids
                or instance_id in self._active_execution_handles
            ),
            release_pause_timeout=lambda instance_id, reason: self._release_pause_timeout_handle(
                instance_id,
                reason=reason,
            ),
            cleanup_task_notes=self._task_note_snapshot_service.async_cleanup_task_note,
        )

    def _set_execution_phase(
        self,
        *,
        task_id: str,
        instance_id: str,
        stream_id: str | None,
        conversation_id: str | None,
        phase: str,
        loop_index: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        now = time.time()
        previous = self._execution_progress.get(instance_id)
        running_tools = previous.running_tools if previous is not None and phase == previous.phase else []
        self._execution_progress[instance_id] = _AgentExecutionProgressState(
            task_id=task_id,
            instance_id=instance_id,
            stream_id=stream_id,
            conversation_id=conversation_id,
            phase=phase,
            loop_index=loop_index,
            phase_started_at=now,
            last_progress_at=now,
            metadata=dict(metadata or {}),
            running_tools=list(running_tools),
        )

    def _touch_execution_progress(
        self,
        instance_id: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        progress = self._execution_progress.get(instance_id)
        if progress is None:
            return
        progress.last_progress_at = time.time()
        if metadata:
            progress.metadata.update(metadata)

    def _set_execution_running_tools(
        self,
        instance_id: str,
        running_tools: list[dict[str, Any]],
    ) -> None:
        progress = self._execution_progress.get(instance_id)
        if progress is None:
            return
        progress.last_progress_at = time.time()
        progress.running_tools = [dict(item) for item in running_tools]

    def _clear_execution_progress(self, instance_id: str) -> None:
        self._execution_progress.pop(instance_id, None)

    async def _validate_file_input_before_enqueue(
        self,
        instance: AgentInstance,
        attachments: tuple[PendingUserMessageAttachment, ...],
    ) -> None:
        if not attachments:
            return
        file_resource_service = (
            self._file_resource_service_getter()
            if self._file_resource_service_getter is not None
            else None
        )
        await validate_agent_instance_file_input_allowed(
            instance=instance,
            new_attachments=attachments,
            conversation_repository=self._conversation_repository,
            file_resource_service=file_resource_service,
        )

    @staticmethod
    def _pause_wake_metadata_from_tool_results(tool_results: list[BaseToolResult]) -> tuple[float, str]:
        """从 pause_task 工具结果元数据中提取结构化唤醒策略。"""
        for result in tool_results:
            metadata = result.metadata or {}
            if "pause_timeout_seconds" not in metadata:
                continue
            raw_timeout = metadata.get("pause_timeout_seconds")
            try:
                timeout_seconds = float(raw_timeout)
            except (TypeError, ValueError):
                logger.warning("忽略非法 pause_timeout_seconds 元数据: %r", raw_timeout)
                continue
            if not math.isfinite(timeout_seconds) or not (timeout_seconds == -1 or timeout_seconds > 0):
                logger.warning("忽略越界 pause_timeout_seconds 元数据: %r", raw_timeout)
                continue
            if timeout_seconds > 0:
                return timeout_seconds, "external_or_timeout"
            return -1.0, "external_only"
        return -1.0, "external_only"

    def _release_pause_timeout_handle(
        self,
        instance_id: str,
        *,
        expected_token: str | None = None,
        cancel_task: bool = True,
        reason: str,
    ) -> _PauseTimeoutHandle | None:
        """释放指定 AgentInstance 的暂停超时句柄；expected_token 用于超时回调幂等竞争。"""
        handle = self._pause_timeout_handles.get(instance_id)
        if handle is None:
            return None
        if expected_token is not None and handle.token != expected_token:
            return None
        self._pause_timeout_handles.pop(instance_id, None)
        if cancel_task and not handle.task.done():
            handle.task.cancel()
        self._record_execution_instance_event(
            instance_id=instance_id,
            stage="pause_timeout.release",
            status="released",
            summary="暂停超时句柄已释放，后续不会再由该句柄触发恢复。",
            details={
                "task_id": handle.task_id,
                "conversation_id": handle.conversation_id,
                "token": handle.token,
                "timeout_seconds": handle.timeout_seconds,
                "deadline": handle.deadline.isoformat(),
                "reason": reason,
                "cancel_task": cancel_task,
            },
        )
        return handle

    def _schedule_pause_timeout(
        self,
        *,
        task_id: str,
        instance_id: str,
        conversation_id: str | None,
        timeout_seconds: float,
        request: AgentExecutionRequest,
        communication_context: RuntimeCommunicationContext,
    ) -> _PauseTimeoutHandle | None:
        """为一次 pause 周期注册进程内超时唤醒。"""
        if timeout_seconds <= 0:
            return None
        if conversation_id is None:
            self._record_execution_instance_event(
                instance_id=instance_id,
                stage="pause_timeout.schedule",
                status="skipped",
                summary="pause_task 请求了超时唤醒，但当前执行没有可复用 conversation_id，已跳过定时器。",
                details={
                    "task_id": task_id,
                    "timeout_seconds": timeout_seconds,
                    "communication_stream_id": communication_context.stream_id,
                    "reason": "missing_conversation_id",
                },
            )
            return None
        self._release_pause_timeout_handle(
            instance_id,
            reason="new_pause_cycle",
        )
        token = uuid.uuid4().hex
        deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
        resume_request = replace(
            request,
            task_id=task_id,
            root_instance_id=instance_id,
            conversation_id=conversation_id,
            communication_stream_id=communication_context.stream_id,
        )
        timeout_task = asyncio.create_task(
            self._pause_timeout_worker(
                instance_id=instance_id,
                token=token,
                timeout_seconds=timeout_seconds,
            )
        )
        handle = _PauseTimeoutHandle(
            token=token,
            task=timeout_task,
            task_id=task_id,
            instance_id=instance_id,
            conversation_id=conversation_id,
            deadline=deadline,
            timeout_seconds=timeout_seconds,
            communication_stream_id=communication_context.stream_id,
            request=resume_request,
        )
        self._pause_timeout_handles[instance_id] = handle
        timeout_task.add_done_callback(
            lambda done_task, instance_id=instance_id, token=token: self._on_pause_timeout_task_done(
                done_task,
                instance_id,
                token,
            )
        )
        self._record_execution_instance_event(
            instance_id=instance_id,
            stage="pause_timeout.schedule",
            status="scheduled",
            summary="pause_task 已注册进程内超时唤醒。",
            details={
                "task_id": task_id,
                "conversation_id": conversation_id,
                "token": token,
                "timeout_seconds": timeout_seconds,
                "deadline": deadline.isoformat(),
                "communication_stream_id": communication_context.stream_id,
            },
            expected_next="external_event_or_timeout_resume",
        )
        return handle

    async def _pause_timeout_worker(
        self,
        *,
        instance_id: str,
        token: str,
        timeout_seconds: float,
    ) -> None:
        try:
            await asyncio.sleep(timeout_seconds)
        except asyncio.CancelledError:
            raise
        await self._resume_agent_instance_from_pause_timeout(
            instance_id=instance_id,
            token=token,
        )

    def _on_pause_timeout_task_done(
        self,
        task: asyncio.Task[None],
        instance_id: str,
        token: str,
    ) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        handle = self._pause_timeout_handles.get(instance_id)
        if handle is not None and handle.token == token and handle.task is task:
            self._pause_timeout_handles.pop(instance_id, None)
        logger.error(
            "Agent pause timeout worker failed: instance_id=%s token=%s",
            instance_id,
            token,
            exc_info=(type(exc), exc, exc.__traceback__),
        )

    def _record_pause_timeout_skip(
        self,
        *,
        instance_id: str,
        token: str,
        reason: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self._record_execution_instance_event(
            instance_id=instance_id,
            stage="pause_timeout.resume",
            status="skipped",
            summary="暂停超时回调未启动恢复执行，因为当前状态已不匹配该暂停周期。",
            details={
                "token": token,
                "reason": reason,
                **(details or {}),
            },
        )

    async def _resume_agent_instance_from_pause_timeout(
        self,
        *,
        instance_id: str,
        token: str,
    ) -> None:
        """超时到期后在 task gate 内恢复同一 AgentInstance 执行。"""
        handle = self._pause_timeout_handles.get(instance_id)
        if handle is None or handle.token != token:
            self._record_pause_timeout_skip(
                instance_id=instance_id,
                token=token,
                reason="handle_missing_or_token_mismatch",
            )
            return
        try:
            task = self._runtime_gateway.get_task(handle.task_id)
        except Exception:
            # 缺失 task 时仍进入既有校验路径，由其释放 handle 并记录精确诊断。
            await self._resume_agent_instance_from_pause_timeout_within_gate(
                instance_id=instance_id,
                token=token,
            )
            return
        async with self._task_operation_gate.hold_key(
            self._task_operation_gate.key_for_task(task),
        ):
            await self._resume_agent_instance_from_pause_timeout_within_gate(
                instance_id=instance_id,
                token=token,
            )

    async def _resume_agent_instance_from_pause_timeout_within_gate(
        self,
        *,
        instance_id: str,
        token: str,
    ) -> None:
        """调用方持有 task gate 后，重读状态并提交 timeout 恢复启动。"""
        handle = self._release_pause_timeout_handle(
            instance_id,
            expected_token=token,
            cancel_task=False,
            reason="timeout_elapsed",
        )
        if handle is None:
            self._record_pause_timeout_skip(
                instance_id=instance_id,
                token=token,
                reason="handle_missing_or_token_mismatch",
            )
            return
        try:
            current_instance = self._runtime_gateway.get_agent_instance(handle.instance_id)
        except Exception as exc:
            self._record_pause_timeout_skip(
                instance_id=handle.instance_id,
                token=token,
                reason="instance_missing",
                details={"error_type": exc.__class__.__name__},
            )
            return
        if current_instance.task_id != handle.task_id:
            self._record_pause_timeout_skip(
                instance_id=handle.instance_id,
                token=token,
                reason="instance_task_mismatch",
                details={
                    "handle_task_id": handle.task_id,
                    "current_task_id": current_instance.task_id,
                },
            )
            return
        try:
            current_task = self._runtime_gateway.get_task(handle.task_id)
        except Exception as exc:
            self._record_pause_timeout_skip(
                instance_id=handle.instance_id,
                token=token,
                reason="task_missing",
                details={"task_id": handle.task_id, "error_type": exc.__class__.__name__},
            )
            return
        if current_task.is_terminal:
            self._record_pause_timeout_skip(
                instance_id=handle.instance_id,
                token=token,
                reason="task_terminal",
                details={"task_id": current_task.task_id, "task_status": current_task.status},
            )
            return
        if current_instance.conversation_id != handle.conversation_id:
            self._record_pause_timeout_skip(
                instance_id=handle.instance_id,
                token=token,
                reason="conversation_mismatch",
                details={
                    "handle_conversation_id": handle.conversation_id,
                    "current_conversation_id": current_instance.conversation_id,
                },
            )
            return
        active_in_memory = (
            handle.instance_id in self._active_instance_ids
            or handle.instance_id in self._active_execution_handles
        )
        if current_instance.status == AGENT_INSTANCE_STATUS_RUNNING or active_in_memory:
            self._record_pause_timeout_skip(
                instance_id=handle.instance_id,
                token=token,
                reason="already_running",
                details={
                    "task_id": current_task.task_id,
                    "instance_status": current_instance.status,
                    "active_in_memory": active_in_memory,
                },
            )
            return
        if current_instance.status != AGENT_INSTANCE_STATUS_READY:
            self._record_pause_timeout_skip(
                instance_id=handle.instance_id,
                token=token,
                reason="instance_not_ready",
                details={
                    "task_id": current_task.task_id,
                    "instance_status": current_instance.status,
                },
            )
            return
        resume_request = replace(
            handle.request,
            thread_id=current_instance.thread_id,
            current_working_directory=current_instance.current_working_directory,
            agent_definition_id=current_instance.agent_definition_id,
            llm_provider_id=current_instance.llm_provider_id,
            llm_model_binding_id=current_instance.llm_model_binding_id,
            task_id=current_task.task_id,
            root_instance_id=current_instance.instance_id,
            conversation_id=current_instance.conversation_id,
            communication_stream_id=handle.communication_stream_id,
        )
        communication_context = self._build_communication_context(resume_request, current_task, current_instance)
        if resume_request.communication_stream_id != communication_context.stream_id:
            resume_request = replace(resume_request, communication_stream_id=communication_context.stream_id)
        self._runtime_gateway.resume_task(current_task.task_id)
        current_instance = self._runtime_gateway.resume_agent_instance(current_instance.instance_id)
        interrupt_state = _AgentExecutionInterruptState()
        task_handle = self._launch_tracked_execution(
            task=current_task,
            instance=current_instance,
            request=resume_request,
            interrupt_state=interrupt_state,
        )
        self._record_execution_instance_event(
            instance_id=current_instance.instance_id,
            stage="pause_timeout.resume",
            status="accepted",
            summary="pause_task 超时已触发同一 AgentInstance 的无用户消息恢复执行。",
            details={
                "task_id": current_task.task_id,
                "conversation_id": current_instance.conversation_id,
                "token": token,
                "timeout_seconds": handle.timeout_seconds,
                "deadline": handle.deadline.isoformat(),
                "communication_stream_id": communication_context.stream_id,
            },
            expected_next="run_agent_instance_without_new_user_message",
        )
        await self._event_publisher.publish(
            communication_context,
            "agent.task.pause_timeout.elapsed",
            {
                "task_id": current_task.task_id,
                "agent_instance_id": current_instance.instance_id,
                "conversation_id": current_instance.conversation_id,
                "pause_token": token,
                "timeout_seconds": handle.timeout_seconds,
                "deadline": handle.deadline.isoformat(),
                "wake_reason": "timeout_elapsed",
            },
            source="agent_execution_engine",
        )

    def _launch_tracked_execution(
        self,
        *,
        task: AgentTask,
        instance: AgentInstance,
        request: AgentExecutionRequest,
        interrupt_state: _AgentExecutionInterruptState | None = None,
    ) -> asyncio.Task[AgentExecutionResult]:
        """原子登记执行活动后创建任务；调用方必须持有对应 Scene/Task gate。"""
        instance_id = instance.instance_id
        if instance_id in self._active_instance_ids or instance_id in self._active_execution_handles:
            raise ValueError(f"Agent 实例已有运行中执行，不能重复启动: {instance_id}")
        actual_interrupt_state = interrupt_state or _AgentExecutionInterruptState()
        self._active_instance_ids.add(instance_id)
        execution_coroutine = self._run_agent_instance(
            task,
            instance,
            request,
            interrupt_state=actual_interrupt_state,
        )
        try:
            task_handle = get_runtime_task_registry().create_task(
                execution_coroutine,
                task_name=f"agent-execution-{_safe_runtime_task_label(instance_id)}",
                owner_type="agent_execution_engine",
                owner_id=instance_id,
                task_class="agent_instance_execution",
                diagnostic_context={
                    "phase": "execution.launch",
                    "agent_task_id": task.task_id,
                    "instance_id": instance_id,
                    "conversation_id": instance.conversation_id,
                    "communication_stream_id": request.communication_stream_id,
                    "agent_definition_id": instance.agent_definition_id,
                },
            )
        except BaseException:
            self._active_instance_ids.discard(instance_id)
            execution_coroutine.close()
            raise
        self._active_execution_handles[instance_id] = _AgentExecutionHandle(
            task=task_handle,
            interrupt_state=actual_interrupt_state,
        )
        self._background_tasks.add(task_handle)
        task_handle.add_done_callback(
            lambda done_task, active_instance_id=instance_id: self._on_background_task_done(
                done_task,
                active_instance_id,
            )
        )
        return task_handle

    async def run_root_agent(self, request: AgentExecutionRequest) -> AgentExecutionResult:
        """创建 task + root Agent 实例，并在 Task gate 内原子登记执行启动。"""
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
        attachments = _request_user_attachments(request)
        await self._validate_file_input_before_enqueue(instance, attachments)
        async with self._task_operation_gate.hold_key(
            self._task_operation_gate.key_for_task(task),
        ):
            task = self._runtime_gateway.get_task(task.task_id)
            instance = self._runtime_gateway.get_agent_instance(instance.instance_id)
            if task.is_terminal:
                raise ValueError(f"Agent 任务已结束，不能启动执行: {task.task_id}")
            if instance.task_id != task.task_id:
                raise ValueError(f"Agent 实例与任务绑定不一致，不能启动执行: {instance.instance_id}")
            self._runtime_gateway.enqueue_agent_instance_user_message(
                instance.instance_id,
                user_text=_request_user_text(request),
                attachments=attachments,
                client_message_id=request.client_message_id,
            )
            instance = self._runtime_gateway.get_agent_instance(instance.instance_id)
            task_handle = self._launch_tracked_execution(
                task=task,
                instance=instance,
                request=request,
            )
        return await task_handle

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
        initial_instance = self._runtime_gateway.get_agent_instance(agent_instance_id)
        initial_task = self._runtime_gateway.get_task(initial_instance.task_id)
        await self._validate_file_input_before_enqueue(initial_instance, attachments)
        async with self._task_operation_gate.hold_key(
            self._task_operation_gate.key_for_task(initial_task),
        ):
            instance = self._runtime_gateway.get_agent_instance(agent_instance_id)
            task = self._runtime_gateway.get_task(instance.task_id)
            if task.is_terminal:
                if task.scene_id is None:
                    raise ValueError(f"Agent 任务已结束，不能继续对话: {task.task_id}")
                task, instance = self._runtime_gateway.prepare_scene_root_agent_instance_for_new_task(
                    instance.instance_id,
                )
            active_in_memory = (
                instance.instance_id in self._active_instance_ids
                or instance.instance_id in self._active_execution_handles
            )
            if instance.status == AGENT_INSTANCE_STATUS_RUNNING and not active_in_memory:
                failure_message = "检测到 Agent 实例持久态为 running，但当前进程没有对应执行任务，已恢复为 ready。"
                instance = self._runtime_gateway.interrupt_agent_instance(
                    instance.instance_id,
                    failure_message,
                )
            if instance.status == AGENT_INSTANCE_STATUS_RUNNING or active_in_memory:
                raise ValueError(f"Agent 实例已有运行中执行，不能重复启动: {instance.instance_id}")
            self._release_pause_timeout_handle(
                instance.instance_id,
                reason="external_user_message",
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
            task_handle = self._launch_tracked_execution(
                task=task,
                instance=instance,
                request=request,
            )
        return await task_handle

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
        message_metadata: dict[str, Any] | None = None,
    ) -> AgentMessageSubmissionResult:
        while True:
            initial_instance = self._runtime_gateway.get_agent_instance(agent_instance_id)
            initial_task = self._runtime_gateway.get_task(initial_instance.task_id)
            if initial_task.is_terminal:
                if initial_task.scene_id is None:
                    raise ValueError(f"Agent 任务已结束，不能继续对话: {initial_task.task_id}")
                async with self._task_operation_gate.hold_key(
                    self._task_operation_gate.key_for_task(initial_task),
                ):
                    current_instance = self._runtime_gateway.get_agent_instance(agent_instance_id)
                    current_task = self._runtime_gateway.get_task(current_instance.task_id)
                    if current_task.is_terminal:
                        self._runtime_gateway.prepare_scene_root_agent_instance_for_new_task(
                            current_instance.instance_id,
                        )
                continue

            await self._validate_file_input_before_enqueue(initial_instance, attachments)
            async with self._task_operation_gate.hold_key(
                self._task_operation_gate.key_for_task(initial_task),
            ):
                current_instance = self._runtime_gateway.get_agent_instance(agent_instance_id)
                current_task = self._runtime_gateway.get_task(current_instance.task_id)
                if current_instance != initial_instance or current_task != initial_task:
                    continue
                return self._submit_agent_instance_message_within_gate(
                    agent_instance_id=agent_instance_id,
                    user_text=user_text,
                    render_options=render_options,
                    request_overrides=request_overrides,
                    communication_stream_id=communication_stream_id,
                    client_message_id=client_message_id,
                    attachments=attachments,
                    message_metadata=message_metadata,
                )

    def _submit_agent_instance_message_within_gate(
        self,
        *,
        agent_instance_id: str,
        user_text: str,
        render_options: dict[str, Any] | None = None,
        request_overrides: dict[str, Any] | None = None,
        communication_stream_id: str | None = None,
        client_message_id: str | None = None,
        attachments: tuple[PendingUserMessageAttachment, ...] = (),
        message_metadata: dict[str, Any] | None = None,
    ) -> AgentMessageSubmissionResult:
        instance = self._runtime_gateway.get_agent_instance(agent_instance_id)
        task = self._runtime_gateway.get_task(instance.task_id)
        if task.is_terminal:
            if task.scene_id is None:
                raise ValueError(f"Agent 任务已结束，不能继续对话: {task.task_id}")
            task, instance = self._runtime_gateway.prepare_scene_root_agent_instance_for_new_task(
                instance.instance_id,
            )
        released_pause_timeout_handle = self._release_pause_timeout_handle(
            instance.instance_id,
            reason="external_user_message",
        )
        instance, pending_message = self._runtime_gateway.enqueue_agent_instance_user_message(
            instance.instance_id,
            user_text=user_text,
            attachments=attachments,
            client_message_id=client_message_id,
            metadata=message_metadata,
        )
        self._record_execution_instance_event(
            instance_id=instance.instance_id,
            stage="message.enqueue",
            status="queued",
            summary="用户消息已进入 AgentInstance 待处理队列。",
            details={
                "task_id": task.task_id,
                "conversation_id": instance.conversation_id,
                "pending_message_id": pending_message.message_id,
                "client_message_id": pending_message.client_message_id,
                "attachment_count": len(attachments),
                "task_terminal_before_enqueue": task.is_terminal,
                "instance_status_after_enqueue": instance.status,
                "released_pause_timeout_token": (
                    released_pause_timeout_handle.token
                    if released_pause_timeout_handle is not None
                    else None
                ),
            },
            expected_next="queue_or_launch_execution",
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
        active_in_memory = (
            instance.instance_id in self._active_instance_ids
            or instance.instance_id in self._active_execution_handles
        )
        if instance.status == AGENT_INSTANCE_STATUS_RUNNING and not active_in_memory:
            failure_message = "检测到 Agent 实例持久态为 running，但当前进程没有对应执行任务，已恢复为 ready。"
            logger.warning(
                "恢复孤儿 Agent 运行态: instance_id=%s, task_id=%s",
                instance.instance_id,
                task.task_id,
            )
            instance = self._runtime_gateway.interrupt_agent_instance(instance.instance_id, failure_message)
            self._record_execution_instance_event(
                instance_id=instance.instance_id,
                stage="orphan_running.repair",
                status="repaired",
                summary="持久态 running 但本进程无执行任务，已恢复为 ready。",
                details={
                    "task_id": task.task_id,
                    "failure_message": failure_message,
                    "active_in_memory_before_repair": active_in_memory,
                    "instance_status_after_repair": instance.status,
                },
                expected_next="queue_or_launch_execution",
            )
        is_running = (
            instance.status == AGENT_INSTANCE_STATUS_RUNNING
            or active_in_memory
        )
        if is_running:
            logger.info(
                "Agent 实例正在运行，新用户消息进入等待队列: instance_id=%s, task_id=%s, pending_message_id=%s, status=%s, active_in_memory=%s",
                instance.instance_id,
                task.task_id,
                pending_message.message_id,
                instance.status,
                active_in_memory,
            )
            self._record_execution_instance_event(
                instance_id=instance.instance_id,
                stage="background_execution.queue",
                status="already_running",
                summary="AgentInstance 已有运行中执行，用户消息保留在队列等待下一轮 drain。",
                details={
                    "task_id": task.task_id,
                    "pending_message_id": pending_message.message_id,
                    "conversation_id": instance.conversation_id,
                    "active_in_memory": active_in_memory,
                    "instance_status": instance.status,
                },
                expected_next="existing_execution_drain_pending_user_messages",
            )
            return AgentMessageSubmissionResult(
                status="queued",
                task_id=task.task_id,
                instance_id=instance.instance_id,
                conversation_id=instance.conversation_id,
                communication_stream_id=communication_context.stream_id,
                pending_message_id=pending_message.message_id,
                client_message_id=pending_message.client_message_id,
                message_metadata=dict(pending_message.metadata),
                queued_for_later=True,
                instance=instance,
            )
        logger.info(
            "Agent 实例启动后台执行: instance_id=%s, task_id=%s, pending_message_id=%s",
            instance.instance_id,
            task.task_id,
            pending_message.message_id,
        )
        interrupt_state = _AgentExecutionInterruptState()
        task_handle = self._launch_tracked_execution(
            task=task,
            instance=instance,
            request=request,
            interrupt_state=interrupt_state,
        )
        self._record_execution_instance_event(
            instance_id=instance.instance_id,
            stage="background_execution.launch",
            status="accepted",
            summary="AgentInstance 后台执行任务已准备启动。",
            details={
                "task_id": task.task_id,
                "conversation_id": instance.conversation_id,
                "pending_message_id": pending_message.message_id,
                "communication_stream_id": communication_context.stream_id,
                "active_instance_count": len(self._active_instance_ids),
            },
            expected_next="run_agent_instance",
        )
        return AgentMessageSubmissionResult(
            status="accepted",
            task_id=task.task_id,
            instance_id=instance.instance_id,
            conversation_id=instance.conversation_id,
            communication_stream_id=communication_context.stream_id,
            pending_message_id=pending_message.message_id,
            client_message_id=pending_message.client_message_id,
            message_metadata=dict(pending_message.metadata),
            queued_for_later=False,
            instance=instance,
        )

    async def complete_current_scene_task(self, scene_id: str) -> SceneTaskCompletionResult:
        """处理用户从 Scene root Agent 面板主动结束当前 Task 会话。"""
        return await self._task_completion_coordinator.complete_current_scene_task(scene_id)

    def interrupt_agent_instance(self, instance_id: str, *, reason: str = "user_requested") -> AgentInterruptResult:
        instance = self._runtime_gateway.get_agent_instance(instance_id)
        task = self._runtime_gateway.get_task(instance.task_id)
        handle = self._active_execution_handles.get(instance_id)
        if handle is None:
            self._record_execution_instance_event(
                instance_id=instance.instance_id,
                stage="interrupt.request",
                status="not_running",
                summary="收到中断请求，但当前进程没有该 AgentInstance 的运行中执行句柄。",
                details={
                    "task_id": task.task_id,
                    "conversation_id": instance.conversation_id,
                    "reason": reason,
                    "instance_status": instance.status,
                },
            )
            return AgentInterruptResult(
                status="not_running",
                task_id=task.task_id,
                instance_id=instance.instance_id,
                conversation_id=instance.conversation_id,
                interrupt_requested=False,
                reason=reason,
            )
        handle.interrupt_state.requested = True
        handle.interrupt_state.reason = reason
        handle.interrupt_state.requested_at = time.perf_counter()
        handle.interrupt_state.cancel_current_llm_requests()
        self._record_execution_instance_event(
            instance_id=instance.instance_id,
            stage="interrupt.request",
            status="requested",
            summary="用户中断请求已写入运行中执行句柄，并已通知当前 LLM 请求取消器。",
            details={
                "task_id": task.task_id,
                "conversation_id": instance.conversation_id,
                "reason": reason,
                "has_active_handle": True,
            },
            expected_next="execution_loop_observe_interrupt",
        )
        return AgentInterruptResult(
            status="interrupt_requested",
            task_id=task.task_id,
            instance_id=instance.instance_id,
            conversation_id=instance.conversation_id,
            interrupt_requested=True,
            reason=reason,
        )

    def _on_background_task_done(self, task: asyncio.Task[AgentExecutionResult], instance_id: str) -> None:
        self._background_tasks.discard(task)
        self._active_instance_ids.discard(instance_id)
        handle = self._active_execution_handles.get(instance_id)
        if handle is not None and handle.task is task:
            self._active_execution_handles.pop(instance_id, None)
        if task.cancelled():
            self._record_execution_instance_event(
                instance_id=instance_id,
                stage="background_task.done",
                status="cancelled",
                summary="Agent 后台执行任务被取消，准备恢复持久运行态。",
                details={
                    "error_type": "CancelledError",
                },
                expected_next="background_exit_recover_instance",
            )
            self._set_execution_phase(
                task_id="",
                instance_id=instance_id,
                stream_id=None,
                conversation_id=None,
                phase="execution_cancelled",
            )
            self._recover_agent_instance_after_background_exit(
                instance_id,
                "Agent 后台执行任务被取消。",
                error_type="CancelledError",
            )
            return
        with suppress(asyncio.CancelledError):
            exc = task.exception()
            if exc is not None:
                failure_message = str(exc) or exc.__class__.__name__
                self._record_execution_instance_event(
                    instance_id=instance_id,
                    stage="background_task.done",
                    status="failed",
                    summary="Agent 后台执行任务异常结束，准备恢复持久运行态。",
                    details={
                        "error_type": exc.__class__.__name__,
                        "error_message": failure_message,
                    },
                    expected_next="background_exit_recover_instance",
                )
                self._set_execution_phase(
                    task_id="",
                    instance_id=instance_id,
                    stream_id=None,
                    conversation_id=None,
                    phase="execution_failed",
                    metadata={
                        "error_type": exc.__class__.__name__,
                        "error_message": failure_message,
                    },
                )
                logger.error("Agent 后台执行失败。", exc_info=(type(exc), exc, exc.__traceback__))
                self._recover_agent_instance_after_background_exit(
                    instance_id,
                    failure_message,
                    error_type=exc.__class__.__name__,
                )
                if _is_process_control_exception(exc):
                    raise exc
                return
            self._record_execution_instance_event(
                instance_id=instance_id,
                stage="background_task.done",
                status="completed",
                summary="Agent 后台执行任务已正常结束并移出活动集合。",
                details={},
            )
            logger.info("Agent 后台执行结束: instance_id=%s", instance_id)

    def _recover_agent_instance_after_background_exit(
        self,
        instance_id: str,
        failure_message: str,
        *,
        error_type: str,
    ) -> None:
        try:
            instance = self._runtime_gateway.get_agent_instance(instance_id)
        except Exception:
            logger.exception("读取异常结束的 Agent 实例失败: instance_id=%s", instance_id)
            return
        if instance.status != AGENT_INSTANCE_STATUS_RUNNING:
            return
        try:
            self._runtime_gateway.interrupt_agent_instance(instance_id, failure_message)
        except Exception:
            logger.exception(
                "恢复异常结束的 Agent 实例状态失败: instance_id=%s, error_type=%s",
                instance_id,
                error_type,
            )

    def _record_execution_instance_event(
        self,
        *,
        instance_id: str,
        stage: str,
        status: str,
        summary: str,
        details: dict[str, Any] | None = None,
        expected_next: str | None = None,
    ) -> None:
        """记录不属于单轮 LLM/tool frame 的实例级执行诊断事件。"""
        try:
            self._execution_loop_logger.record_instance_event(
                instance_id=instance_id,
                stage=stage,
                status=status,
                summary=summary,
                details=details or {},
                expected_next=expected_next,
            )
        except Exception:  # noqa: BLE001 - 诊断不能影响 Agent 热路径
            logger.debug(
                "记录 Agent 执行循环实例诊断事件失败: instance_id=%s stage=%s",
                instance_id,
                stage,
                exc_info=True,
            )

    def _begin_execution_loop_frame(
        self,
        *,
        task: AgentTask,
        instance: AgentInstance,
        loop_index: int,
        conversation_id: str | None,
        communication_context: RuntimeCommunicationContext,
        details: dict[str, Any] | None = None,
        expected_next: str | None = None,
    ) -> str | None:
        """开始记录一次从 LLM 请求到下一步决策的滚动诊断 frame。"""
        try:
            return self._execution_loop_logger.begin_frame(
                task_id=task.task_id,
                instance_id=instance.instance_id,
                loop_index=loop_index,
                conversation_id=conversation_id,
                stream_id=communication_context.stream_id,
                details=details or {},
                expected_next=expected_next,
            )
        except Exception:  # noqa: BLE001 - 诊断不能影响 Agent 热路径
            logger.debug(
                "开始 Agent 执行循环诊断 frame 失败: instance_id=%s loop_index=%s",
                instance.instance_id,
                loop_index,
                exc_info=True,
            )
            return None

    def _record_execution_loop_step(
        self,
        *,
        instance_id: str,
        stage: str,
        status: str,
        summary: str,
        details: dict[str, Any] | None = None,
        expected_next: str | None = None,
        frame_id: str | None = None,
    ) -> None:
        """记录当前执行循环 frame 内的一个诊断步骤。"""
        try:
            self._execution_loop_logger.record_step(
                instance_id=instance_id,
                frame_id=frame_id,
                stage=stage,
                status=status,
                summary=summary,
                details=details or {},
                expected_next=expected_next,
            )
        except Exception:  # noqa: BLE001 - 诊断不能影响 Agent 热路径
            logger.debug(
                "记录 Agent 执行循环诊断步骤失败: instance_id=%s stage=%s",
                instance_id,
                stage,
                exc_info=True,
            )

    def _update_execution_frame_metadata(
        self,
        *,
        instance_id: str,
        frame_id: str | None,
        updates: dict[str, Any],
    ) -> None:
        """更新当前执行循环 frame 元数据。"""
        try:
            self._execution_loop_logger.update_frame_metadata(
                instance_id=instance_id,
                frame_id=frame_id,
                updates=updates,
            )
        except Exception:  # noqa: BLE001 - 诊断不能影响 Agent 热路径
            logger.debug(
                "更新 Agent 执行循环 frame 元数据失败: instance_id=%s frame_id=%s",
                instance_id,
                frame_id,
                exc_info=True,
            )

    def _record_execution_llm_attempt(
        self,
        *,
        instance_id: str,
        frame_id: str | None,
        attempt: dict[str, Any],
    ) -> None:
        """记录当前 frame 关联的一次 LLM request attempt。"""
        try:
            self._execution_loop_logger.record_llm_attempt(
                instance_id=instance_id,
                frame_id=frame_id,
                attempt=attempt,
            )
        except Exception:  # noqa: BLE001 - 诊断不能影响 Agent 热路径
            logger.debug(
                "记录 Agent 执行循环 LLM attempt 失败: instance_id=%s frame_id=%s",
                instance_id,
                frame_id,
                exc_info=True,
            )

    def _record_execution_conversation_rebind(
        self,
        *,
        instance_id: str,
        frame_id: str | None,
        reason: str,
        job_id: str | None,
        source_conversation_id: str | None,
        target_conversation_id: str | None,
    ) -> None:
        """记录当前 frame 内的 conversation rebind。"""
        try:
            self._execution_loop_logger.record_conversation_rebind(
                instance_id=instance_id,
                frame_id=frame_id,
                reason=reason,
                job_id=job_id,
                source_conversation_id=source_conversation_id,
                target_conversation_id=target_conversation_id,
            )
        except Exception:  # noqa: BLE001 - 诊断不能影响 Agent 热路径
            logger.debug(
                "记录 Agent 执行循环 conversation rebind 失败: instance_id=%s frame_id=%s",
                instance_id,
                frame_id,
                exc_info=True,
            )

    def _end_execution_loop_frame(
        self,
        *,
        instance_id: str,
        frame_id: str | None,
        outcome: str,
        summary: str,
        details: dict[str, Any] | None = None,
        expected_next: str | None = None,
    ) -> None:
        """结束当前滚动诊断 frame，并尽力触发异步落盘。"""
        try:
            self._execution_loop_logger.end_frame(
                instance_id=instance_id,
                frame_id=frame_id,
                outcome=outcome,
                summary=summary,
                details=details or {},
                expected_next=expected_next,
            )
        except Exception:  # noqa: BLE001 - 诊断不能影响 Agent 热路径
            logger.debug(
                "结束 Agent 执行循环诊断 frame 失败: instance_id=%s frame_id=%s",
                instance_id,
                frame_id,
                exc_info=True,
            )

    async def _run_agent_instance(
        self,
        task: AgentTask,
        instance: AgentInstance,
        request: AgentExecutionRequest,
        *,
        interrupt_state: _AgentExecutionInterruptState | None = None,
    ) -> AgentExecutionResult:
        interrupt_state = interrupt_state or _AgentExecutionInterruptState()
        communication_context = self._build_communication_context(request, task, instance)
        trace_path = self._trace_recorder.start_trace(_trace_identity(instance))
        conversation: Any | None = None
        loop_index = 0
        self._set_execution_phase(
            task_id=task.task_id,
            instance_id=instance.instance_id,
            stream_id=communication_context.stream_id,
            conversation_id=request.conversation_id or instance.conversation_id,
            phase="starting",
        )
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
            self._set_execution_phase(
                task_id=task.task_id,
                instance_id=instance.instance_id,
                stream_id=communication_context.stream_id,
                conversation_id=request.conversation_id or instance.conversation_id,
                phase="load_conversation",
            )
            conversation = await self._load_or_create_conversation(instance, request)
            self._record_execution_instance_event(
                instance_id=instance.instance_id,
                stage="conversation.loaded",
                status="ready",
                summary="Agent 执行已加载或创建会话。",
                details={
                    "task_id": task.task_id,
                    "conversation_id": conversation.id,
                    "message_count": len(conversation.messages),
                    "trace_path": str(trace_path),
                },
                expected_next="repair_unmatched_tool_results",
            )
            repaired_unmatched_tool_results = await self._repair_unmatched_tool_results(
                conversation,
                reason="previous_execution_interrupted",
            )
            if repaired_unmatched_tool_results:
                self._record_execution_instance_event(
                    instance_id=instance.instance_id,
                    stage="tool_results.repair",
                    status="repaired",
                    summary="启动时发现上一轮 assistant 工具调用缺少 tool 结果，已补齐聚合 ROLE_TOOL 消息。",
                    details={
                        "task_id": task.task_id,
                        "conversation_id": conversation.id,
                        "reason": "previous_execution_interrupted",
                        "message_count_after_repair": len(conversation.messages),
                    },
                    expected_next="loop_start",
                )
            communication_context = communication_context.with_metadata(conversation_id=conversation.id)
            self._touch_execution_progress(instance.instance_id, metadata={"conversation_id": conversation.id})

            plain_text_fallback_count = 0
            while True:
                loop_index += 1
                instance = self._runtime_gateway.get_agent_instance(instance.instance_id)
                self._set_execution_phase(
                    task_id=task.task_id,
                    instance_id=instance.instance_id,
                    stream_id=communication_context.stream_id,
                    conversation_id=conversation.id,
                    phase="loop_start",
                    loop_index=loop_index,
                    metadata={"pending_user_messages": len(instance.pending_user_messages)},
                )
                logger.info(
                    "Agent 循环轮次开始: task_id=%s, instance_id=%s, loop_index=%s, status=%s, pending_user_messages=%s",
                    task.task_id,
                    instance.instance_id,
                    loop_index,
                    instance.status,
                    len(instance.pending_user_messages),
                )
                self._set_execution_phase(
                    task_id=task.task_id,
                    instance_id=instance.instance_id,
                    stream_id=communication_context.stream_id,
                    conversation_id=conversation.id,
                    phase="drain_user_messages",
                    loop_index=loop_index,
                )
                pending_user_message_count = len(instance.pending_user_messages)
                if pending_user_message_count:
                    communication_context = communication_context.with_metadata(
                        conversation_history_copy_message_count=len(conversation.messages),
                    )
                drained_count = await self._drain_pending_user_messages_to_conversation(
                    instance_id=instance.instance_id,
                    conversation=conversation,
                    communication_context=communication_context,
                    trace_path=trace_path,
                )
                self._touch_execution_progress(instance.instance_id, metadata={"drained_user_message_count": drained_count})
                self._record_execution_instance_event(
                    instance_id=instance.instance_id,
                    stage="pending_user_messages.drain",
                    status="drained" if drained_count else "empty",
                    summary="执行循环在下一次 LLM 请求前完成待处理用户消息队列消费。",
                    details={
                        "task_id": task.task_id,
                        "conversation_id": conversation.id,
                        "loop_index": loop_index,
                        "drained_count": drained_count,
                        "message_count_after_drain": len(conversation.messages),
                    },
                    expected_next="llm_frame_start",
                )
                instance = self._runtime_gateway.get_agent_instance(instance.instance_id)
                resolved_candidates, unsupported_modality_strategy = self._resolve_instance_llm_policy(instance)
                agent_definition = self._load_agent_definition(instance.agent_definition_id)
                current_frame_id = self._begin_execution_loop_frame(
                    task=task,
                    instance=instance,
                    loop_index=loop_index,
                    conversation_id=conversation.id,
                    communication_context=communication_context,
                    details={
                        "candidate_count": len(resolved_candidates),
                        "unsupported_modality_strategy": unsupported_modality_strategy,
                        "conversation_message_count": len(conversation.messages),
                        "drained_user_message_count": drained_count,
                        "agent_definition_id": agent_definition.agent_id,
                    },
                    expected_next="send_llm_prompt",
                )
                try:
                    self._set_execution_phase(
                        task_id=task.task_id,
                        instance_id=instance.instance_id,
                        stream_id=communication_context.stream_id,
                        conversation_id=conversation.id,
                        phase="llm_prompt",
                        loop_index=loop_index,
                        metadata={
                            "candidate_count": len(resolved_candidates),
                        },
                    )
                    self._record_execution_loop_step(
                        instance_id=instance.instance_id,
                        frame_id=current_frame_id,
                        stage="llm.request.start",
                        status="started",
                        summary="准备渲染系统提示词、构建工具 schema 并向 LLM 发送请求。",
                        details={
                            "task_id": task.task_id,
                            "conversation_id": conversation.id,
                            "loop_index": loop_index,
                            "candidate_count": len(resolved_candidates),
                            "unsupported_modality_strategy": unsupported_modality_strategy,
                        },
                        expected_next="llm.response.received",
                    )
                    prompt_result = await _await_with_progress_logging(
                        self._send_prompt_with_policy_failover(
                            task=task,
                            trace_path=trace_path,
                            conversation=conversation,
                            agent_definition=agent_definition,
                            instance=instance,
                            resolved_candidates=resolved_candidates,
                            unsupported_modality_strategy=unsupported_modality_strategy,
                            render_options=request.render_options,
                            request_overrides=request.request_overrides,
                            communication_context=communication_context,
                            interrupt_state=interrupt_state,
                            execution_frame_id=current_frame_id,
                        ),
                        stage="llm_prompt",
                        task_id=task.task_id,
                        instance_id=instance.instance_id,
                        metadata={
                            "loop_index": loop_index,
                            "conversation_id": conversation.id,
                            "candidate_count": len(resolved_candidates),
                        },
                        progress_callback=lambda _elapsed_ms, instance_id=instance.instance_id: self._touch_execution_progress(
                            instance_id,
                        ),
                    )
                except LlmRequestCancelled:
                    self._end_execution_loop_frame(
                        instance_id=instance.instance_id,
                        frame_id=current_frame_id,
                        outcome="interrupted",
                        summary="LLM 请求被用户中断，执行循环将暂停并等待后续用户消息。",
                        details={
                            "task_id": task.task_id,
                            "conversation_id": conversation.id,
                            "loop_index": loop_index,
                            "interrupt_reason": interrupt_state.reason,
                        },
                        expected_next="pause_for_user_interrupt",
                    )
                    return await self._pause_for_user_interrupt(
                        task=task,
                        instance=instance,
                        conversation_id=conversation.id,
                        trace_path=trace_path,
                        communication_context=communication_context,
                        interrupt_state=interrupt_state,
                        assistant_message=None,
                    )
                llm_response = prompt_result.response
                conversation = prompt_result.conversation
                instance = prompt_result.instance
                communication_context = communication_context.with_metadata(
                    conversation_id=conversation.id,
                    conversation_history_copy_message_count=prompt_result.conversation_history_copy_message_count,
                )
                assistant_message = llm_response.assistant_message
                self._record_execution_loop_step(
                    instance_id=instance.instance_id,
                    frame_id=current_frame_id,
                    stage="llm.response.received",
                    status="received",
                    summary="LLM 返回 assistant 消息，准备持久化并解析工具调用。",
                    details={
                        "task_id": task.task_id,
                        "conversation_id": conversation.id,
                        "loop_index": loop_index,
                        "assistant_part_count": len(assistant_message.content),
                        "tool_use_count": len(_extract_tool_uses(assistant_message)),
                    },
                    expected_next="assistant_message.persisted",
                )
                _record_last_context_usage(conversation, llm_response)
                self._set_execution_phase(
                    task_id=task.task_id,
                    instance_id=instance.instance_id,
                    stream_id=communication_context.stream_id,
                    conversation_id=conversation.id,
                    phase="persist_assistant_message",
                    loop_index=loop_index,
                )
                await self._conversation_repository.save(conversation)
                self._record_execution_loop_step(
                    instance_id=instance.instance_id,
                    frame_id=current_frame_id,
                    stage="assistant_message.persisted",
                    status="saved",
                    summary="assistant 消息已写入 Conversation，并即将发布前端/运行时事件。",
                    details={
                        "task_id": task.task_id,
                        "conversation_id": conversation.id,
                        "loop_index": loop_index,
                        "assistant_message_index": len(conversation.messages) - 1,
                        "message_count_after_save": len(conversation.messages),
                    },
                    expected_next="assistant_message.handle_tools",
                )
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
                self._record_execution_loop_step(
                    instance_id=instance.instance_id,
                    frame_id=current_frame_id,
                    stage="assistant_message.tool_uses.extracted",
                    status="ready",
                    summary="assistant 消息已完成工具调用解析，准备进入工具批执行或 plain-text fallback。",
                    details={
                        "task_id": task.task_id,
                        "conversation_id": conversation.id,
                        "loop_index": loop_index,
                        "tool_count": len(tool_uses),
                        "tool_call_ids": [_tool_use_ref(tool_use) for tool_use in tool_uses],
                        "tool_names": [tool_use.name for tool_use in tool_uses],
                    },
                    expected_next="tool_batch.start" if tool_uses else "plain_text_fallback",
                )
                if interrupt_state.requested:
                    if tool_uses:
                        tool_result_message, tool_results = await self._build_interrupted_tool_results(
                            tool_uses,
                            communication_context,
                            interrupt_state=interrupt_state,
                            phase="before_tool_start",
                        )
                        conversation.append_message(tool_result_message)
                        await self._conversation_repository.save(conversation)
                        self._trace_recorder.record_tool_result(trace_path, tool_result_message)
                    self._end_execution_loop_frame(
                        instance_id=instance.instance_id,
                        frame_id=current_frame_id,
                        outcome="interrupted",
                        summary="assistant 响应已落库，但工具执行前收到用户中断，执行循环将暂停。",
                        details={
                            "task_id": task.task_id,
                            "conversation_id": conversation.id,
                            "loop_index": loop_index,
                            "tool_count": len(tool_uses),
                            "interrupt_reason": interrupt_state.reason,
                            "aggregated_interrupted_tool_message_saved": bool(tool_uses),
                        },
                        expected_next="pause_for_user_interrupt",
                    )
                    return await self._pause_for_user_interrupt(
                        task=task,
                        instance=instance,
                        conversation_id=conversation.id,
                        trace_path=trace_path,
                        communication_context=communication_context,
                        interrupt_state=interrupt_state,
                        assistant_message=assistant_message,
                    )
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
                    self._record_execution_loop_step(
                        instance_id=instance.instance_id,
                        frame_id=current_frame_id,
                        stage="plain_text_fallback",
                        status="threshold_reached" if plain_text_fallback_count >= _PLAIN_TEXT_FALLBACK_PAUSE_THRESHOLD else "retry",
                        summary="assistant 未返回工具调用，Runtime 按 plain-text fallback 策略处理。",
                        details={
                            "task_id": task.task_id,
                            "conversation_id": conversation.id,
                            "loop_index": loop_index,
                            "fallback_count": plain_text_fallback_count,
                            "threshold": _PLAIN_TEXT_FALLBACK_PAUSE_THRESHOLD,
                        },
                        expected_next="pause_task" if plain_text_fallback_count >= _PLAIN_TEXT_FALLBACK_PAUSE_THRESHOLD else "next_llm_request",
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
                        self._end_execution_loop_frame(
                            instance_id=instance.instance_id,
                            frame_id=current_frame_id,
                            outcome="paused",
                            summary="plain-text fallback 连续达到阈值，Runtime 已暂停 task/instance。",
                            details={
                                "task_id": task.task_id,
                                "conversation_id": conversation.id,
                                "loop_index": loop_index,
                                "fallback_count": plain_text_fallback_count,
                                "threshold": _PLAIN_TEXT_FALLBACK_PAUSE_THRESHOLD,
                            },
                            expected_next="resume_from_pending_user_messages",
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
                    self._end_execution_loop_frame(
                        instance_id=instance.instance_id,
                        frame_id=current_frame_id,
                        outcome="continue",
                        summary="plain-text fallback 未达到暂停阈值，执行循环将继续请求下一轮 LLM。",
                        details={
                            "task_id": task.task_id,
                            "conversation_id": conversation.id,
                            "loop_index": loop_index,
                            "fallback_count": plain_text_fallback_count,
                            "threshold": _PLAIN_TEXT_FALLBACK_PAUSE_THRESHOLD,
                        },
                        expected_next="next_llm_request",
                    )
                    continue
                plain_text_fallback_count = 0
                assistant_message_index = len(conversation.messages) - 1

                self._set_execution_phase(
                    task_id=task.task_id,
                    instance_id=instance.instance_id,
                    stream_id=communication_context.stream_id,
                    conversation_id=conversation.id,
                    phase="tool_batch",
                    loop_index=loop_index,
                    metadata={
                        "tool_count": len(tool_uses),
                        "tool_names": [tool_use.name for tool_use in tool_uses],
                    },
                )
                self._record_execution_loop_step(
                    instance_id=instance.instance_id,
                    frame_id=current_frame_id,
                    stage="tool_batch.start",
                    status="started",
                    summary="开始执行 assistant 消息中的工具批次。",
                    details={
                        "task_id": task.task_id,
                        "conversation_id": conversation.id,
                        "loop_index": loop_index,
                        "assistant_message_index": assistant_message_index,
                        "tool_count": len(tool_uses),
                        "tool_call_ids": [_tool_use_ref(tool_use) for tool_use in tool_uses],
                        "tool_names": [tool_use.name for tool_use in tool_uses],
                        "expected_durability_order": [
                            "persist_each_finished_tool_result_as_pending",
                            "append_and_save_aggregated_role_tool_message",
                            "clear_pending_tool_result_rows",
                            "select_and_apply_loop_decision",
                        ],
                    },
                    expected_next="tool_batch.returned",
                )
                tool_result_message, tool_results = await _await_with_progress_logging(
                    self._execute_tool_uses(
                        agent_definition,
                        tool_uses,
                        communication_context,
                        conversation_id=conversation.id,
                        assistant_message_index=assistant_message_index,
                        interrupt_state=interrupt_state,
                        instance=instance,
                    ),
                    stage="tool_batch",
                    task_id=task.task_id,
                    instance_id=instance.instance_id,
                    metadata={
                        "loop_index": loop_index,
                        "tool_count": len(tool_uses),
                        "tool_names": [tool_use.name for tool_use in tool_uses],
                    },
                    progress_callback=lambda _elapsed_ms, instance_id=instance.instance_id: self._touch_execution_progress(
                        instance_id,
                    ),
                )
                self._record_execution_loop_step(
                    instance_id=instance.instance_id,
                    frame_id=current_frame_id,
                    stage="tool_batch.returned",
                    status="completed",
                    summary="_execute_tool_uses 已返回，所有完成的工具结果应已先作为 pending tool result 落库。",
                    details={
                        "task_id": task.task_id,
                        "conversation_id": conversation.id,
                        "loop_index": loop_index,
                        "assistant_message_index": assistant_message_index,
                        "tool_result_count": len(tool_results),
                        "tool_result_refs": [part.tool_use_ref for part in tool_result_message.content],
                        "loop_decisions": [result.loop_decision for result in tool_results],
                        "conversation_message_count_before_aggregated_tool_message": len(conversation.messages),
                        "next_durability_step": "append_and_save_aggregated_role_tool_message",
                    },
                    expected_next="tool_results.aggregate_message.saved",
                )
                self._set_execution_phase(
                    task_id=task.task_id,
                    instance_id=instance.instance_id,
                    stream_id=communication_context.stream_id,
                    conversation_id=conversation.id,
                    phase="persist_tool_results",
                    loop_index=loop_index,
                )
                tool_message_index = len(conversation.messages)
                conversation.append_message(tool_result_message)
                await self._conversation_repository.save(conversation)
                self._record_execution_loop_step(
                    instance_id=instance.instance_id,
                    frame_id=current_frame_id,
                    stage="tool_results.aggregate_message.saved",
                    status="saved",
                    summary="聚合 ROLE_TOOL 消息已追加并持久化到 Conversation。",
                    details={
                        "task_id": task.task_id,
                        "conversation_id": conversation.id,
                        "loop_index": loop_index,
                        "assistant_message_index": assistant_message_index,
                        "aggregated_tool_message_index": tool_message_index,
                        "tool_result_part_count": len(tool_result_message.content),
                        "message_count_after_save": len(conversation.messages),
                        "invariants": {
                            "tool_result_count_matches_tool_uses": len(tool_results) == len(tool_uses),
                            "tool_result_part_count_matches_tool_uses": len(tool_result_message.content) == len(tool_uses),
                            "tool_message_follows_assistant_message": tool_message_index == assistant_message_index + 1,
                        },
                    },
                    expected_next="pending_tool_results.clear",
                )
                await self._clear_pending_tool_results(conversation.id, assistant_message_index)
                self._record_execution_loop_step(
                    instance_id=instance.instance_id,
                    frame_id=current_frame_id,
                    stage="pending_tool_results.cleared",
                    status="cleared",
                    summary="与 assistant 消息对应的 pending tool result 行已清理。",
                    details={
                        "task_id": task.task_id,
                        "conversation_id": conversation.id,
                        "loop_index": loop_index,
                        "assistant_message_index": assistant_message_index,
                        "aggregated_tool_message_index": tool_message_index,
                    },
                    expected_next="loop_decision.select",
                )
                self._trace_recorder.record_tool_result(trace_path, tool_result_message)
                loop_decision = _select_loop_decision(tool_results)
                has_note_update_end = any(tu.name == "note_update_end" for tu in tool_uses)
                if has_note_update_end and instance.execution_substate == EXECUTION_SUBSTATE_NOTE_UPDATE:
                    self._record_execution_loop_step(
                        instance_id=instance.instance_id,
                        frame_id=current_frame_id,
                        stage="task_note.update_end.detected",
                        status="started",
                        summary="检测到 note_update_end 工具调用，开始构建全新会话并替换 Conversation。",
                        details={
                            "task_id": task.task_id,
                            "old_conversation_id": conversation.id,
                            "instance_id": instance.instance_id,
                        },
                        expected_next="task_note.conversation.swap",
                    )
                    # 1. 从 PostgreSQL 读取当前 agent 实例 + task_id 的最新完整 Note 全量快照 JSON
                    latest_note_snapshot = await self._task_note_snapshot_service.build_task_note_snapshot_json(
                        task_id=task.task_id,
                        agent_instance_id=instance.instance_id,
                    )

                    # 2. 调用会话仓储创建全新的 Conversation
                    new_conversation_id = f"conv_{uuid.uuid4().hex[:12]}"
                    new_conversation = Conversation(id=new_conversation_id)

                    # 3. 在新会话的 timeline 中初始化业务 Agent 定义的 Prompt 与 Tools
                    business_tools = await self._tool_dispatcher.build_llm_tools(
                        agent_definition=agent_definition,
                        api_format=resolved_candidates[0].api_format,
                    )
                    business_system_prompt = self._render_system_prompt(
                        agent_definition,
                        instance.instance_id,
                        instance.current_working_directory,
                        request.render_options,
                        business_tools,
                    )
                    new_conversation.system_prompt = business_system_prompt
                    new_conversation.tools = tuple(
                        deepcopy(t.schema) if hasattr(t, "schema") else deepcopy(t)
                        for t in business_tools
                    )
                    new_conversation.record_configuration_event(
                        ConfigurationComponentEvent(
                            event_id=f"evt_prompt_{uuid.uuid4().hex[:8]}",
                            definition_id=agent_definition.agent_id,
                            anchor="start",
                            sequence=1,
                            status="active",
                            component="prompt",
                        )
                    )
                    new_conversation.record_configuration_event(
                        ConfigurationComponentEvent(
                            event_id=f"evt_tool_{uuid.uuid4().hex[:8]}",
                            definition_id=agent_definition.agent_id,
                            anchor="start",
                            sequence=0,
                            status="active",
                            component="tool",
                        )
                    )

                    # 4. 注入包含 Note 快照与业务唤醒指示的初始消息（反查源码行号与命令行指令，组织为 Markdown 工作手记）
                    resumed_prompt = self._task_note_prompt_provider.build_resumed_conversation_prompt(
                        note_snapshot=latest_note_snapshot,
                        current_working_directory=instance.current_working_directory,
                        task_id=task.task_id,
                        agent_instance_id=instance.instance_id,
                    )
                    resumed_msg = UniMessage(
                        role=ROLE_USER,
                        content=[TextPart(text=resumed_prompt)],
                        message_id=f"msg_{uuid.uuid4().hex[:12]}",
                    )
                    new_conversation.append_message(resumed_msg, [TextPartMeta()])
                    await self._conversation_repository.create(
                        new_conversation,
                        task_id=task.task_id,
                        thread_id=instance.thread_id,
                        agent_id=agent_definition.agent_id,
                        agent_instance_id=instance.instance_id,
                    )

                    # 5. 调用 switch_agent_instance_conversation
                    self._runtime_gateway.switch_agent_instance_conversation(instance.instance_id, new_conversation.id)

                    # 6. 更新实例状态：instance = instance.exit_note_update()，写回 SQLite
                    instance = instance.exit_note_update()
                    self._runtime_gateway.update_agent_instance_execution_substate(instance.instance_id, None)

                    # 7. 切换会话引用与上下文
                    old_conversation_id = conversation.id
                    conversation = new_conversation
                    communication_context = communication_context.with_metadata(conversation_id=conversation.id)

                    # 8. 记录事件与日志
                    self._record_execution_conversation_rebind(
                        instance_id=instance.instance_id,
                        frame_id=current_frame_id,
                        reason="task_note_update_completed",
                        job_id=None,
                        source_conversation_id=old_conversation_id,
                        target_conversation_id=new_conversation.id,
                    )
                    self._trace_recorder.record_event(
                        trace_path,
                        "task_note_update_conversation_swapped",
                        {
                            "old_conversation_id": old_conversation_id,
                            "new_conversation_id": new_conversation.id,
                            "task_id": task.task_id,
                            "agent_instance_id": instance.instance_id,
                        },
                    )
                    await self._event_publisher.publish(
                        communication_context,
                        "agent.conversation.swapped",
                        {
                            "task_id": task.task_id,
                            "agent_instance_id": instance.instance_id,
                            "old_conversation_id": old_conversation_id,
                            "new_conversation_id": new_conversation.id,
                            "reason": "task_note_update_completed",
                        },
                        source="agent_execution_engine",
                    )

                    # 9. 强制收敛决策为 continue，让循环继续进入新会话
                    loop_decision = "continue"
                self._record_execution_loop_step(
                    instance_id=instance.instance_id,
                    frame_id=current_frame_id,
                    stage="loop_decision.selected",
                    status=loop_decision,
                    summary="Runtime 已根据工具结果优先级选出本轮最终 loop decision。",
                    details={
                        "task_id": task.task_id,
                        "conversation_id": conversation.id,
                        "loop_index": loop_index,
                        "tool_count": len(tool_results),
                        "loop_decision": loop_decision,
                        "loop_decisions": [result.loop_decision for result in tool_results],
                        "priority_order": ["continue", "pause", "complete", "fail"],
                        "expected_next_step": "check_interrupt_then_apply_or_continue",
                    },
                    expected_next="interrupt_check",
                )
                logger.info(
                    "Agent 循环工具结果已落库: task_id=%s, instance_id=%s, loop_index=%s, tool_count=%s, loop_decision=%s",
                    task.task_id,
                    instance.instance_id,
                    loop_index,
                    len(tool_results),
                    loop_decision,
                )
                if interrupt_state.requested:
                    self._end_execution_loop_frame(
                        instance_id=instance.instance_id,
                        frame_id=current_frame_id,
                        outcome="interrupted",
                        summary="工具结果已完成持久化，但 loop decision 应用前收到用户中断，执行循环将暂停。",
                        details={
                            "task_id": task.task_id,
                            "conversation_id": conversation.id,
                            "loop_index": loop_index,
                            "tool_count": len(tool_results),
                            "loop_decision_before_interrupt": loop_decision,
                            "interrupt_reason": interrupt_state.reason,
                        },
                        expected_next="pause_for_user_interrupt",
                    )
                    return await self._pause_for_user_interrupt(
                        task=task,
                        instance=instance,
                        conversation_id=conversation.id,
                        trace_path=trace_path,
                        communication_context=communication_context,
                        interrupt_state=interrupt_state,
                        assistant_message=assistant_message,
                    )
                if loop_decision != "continue":
                    await self._apply_loop_decision(
                        loop_decision=loop_decision,
                        task_id=task.task_id,
                        instance_id=instance.instance_id,
                        trace_path=trace_path,
                        tool_results=tool_results,
                        communication_context=communication_context,
                        request=request,
                    )
                    self._record_execution_loop_step(
                        instance_id=instance.instance_id,
                        frame_id=current_frame_id,
                        stage="loop_decision.applied",
                        status=loop_decision,
                        summary="非 continue loop decision 已应用到 task/instance 运行态。",
                        details={
                            "task_id": task.task_id,
                            "conversation_id": conversation.id,
                            "loop_index": loop_index,
                            "loop_decision": loop_decision,
                        },
                        expected_next="resume_from_pending_user_messages",
                    )
                    self._end_execution_loop_frame(
                        instance_id=instance.instance_id,
                        frame_id=current_frame_id,
                        outcome=loop_decision,
                        summary="本轮执行已按非 continue loop decision 收敛，准备检查是否有排队用户消息。",
                        details={
                            "task_id": task.task_id,
                            "conversation_id": conversation.id,
                            "loop_index": loop_index,
                            "loop_decision": loop_decision,
                            "tool_count": len(tool_results),
                        },
                        expected_next="resume_from_pending_user_messages",
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
                self._end_execution_loop_frame(
                    instance_id=instance.instance_id,
                    frame_id=current_frame_id,
                    outcome="continue",
                    summary="工具结果已重新插入 Conversation，loop decision 为 continue，准备进入下一轮 LLM 请求。",
                    details={
                        "task_id": task.task_id,
                        "conversation_id": conversation.id,
                        "loop_index": loop_index,
                        "tool_count": len(tool_results),
                        "loop_decision": loop_decision,
                    },
                    expected_next="next_llm_request",
                )

            raise RuntimeError("Agent 执行循环异常结束。")
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if _is_process_control_exception(exc):
                raise
            failure_message = str(exc) or exc.__class__.__name__
            self._set_execution_phase(
                task_id=task.task_id,
                instance_id=instance.instance_id,
                stream_id=communication_context.stream_id,
                conversation_id=conversation.id if conversation is not None else request.conversation_id,
                phase="execution_failed",
                loop_index=loop_index or None,
                metadata={
                    "error_type": exc.__class__.__name__,
                    "error_message": failure_message,
                },
            )
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
                next_execution = await self._resume_from_pending_user_messages(
                    task=current_task,
                    instance=current_instance,
                    request=request,
                    conversation=conversation,
                    communication_context=communication_context,
                    trace_path=trace_path,
                )
                if next_execution is not None:
                    (
                        next_task,
                        next_instance,
                        next_request,
                        _next_conversation,
                        _next_context,
                        _next_trace_path,
                    ) = next_execution
                    # _resume_from_pending_user_messages 已在 gate 内重读并登记 running；
                    # 当前 tracked execution reservation 仍在，完成入口会观察到 busy。
                    return await self._run_agent_instance(next_task, next_instance, next_request)
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

    async def _repair_unmatched_tool_results(
        self,
        conversation: Any,
        *,
        reason: str,
    ) -> bool:
        if not conversation.messages:
            return False
        assistant_message_index = len(conversation.messages) - 1
        last_message = conversation.messages[assistant_message_index]
        if last_message.role != "assistant":
            return False
        tool_uses = _extract_tool_uses(last_message)
        if not tool_uses:
            return False
        pending_tool_results = await self._list_pending_tool_results(
            conversation.id,
            assistant_message_index,
        )
        pending_tool_results_by_ref = {
            result.tool_use_ref: result
            for result in pending_tool_results
        }
        result_parts: list[ToolResultPart] = []
        for tool_use in tool_uses:
            tool_use_ref = _tool_use_ref(tool_use)
            pending_result = pending_tool_results_by_ref.get(tool_use_ref)
            if pending_result is not None:
                result_parts.append(pending_result)
                continue
            result_parts.append(
                ToolResultPart(
                    tool_use_ref=tool_use_ref,
                    is_error=True,
                    output=normalize_tool_result_output(
                        "工具执行失败：Tool execution did not complete because the previous Agent execution was interrupted. "
                        f"reason={reason}; tool_name={tool_use.name}"
                    ),
                )
            )
        tool_message = UniMessage(
            role=ROLE_TOOL,
            content=result_parts,
        )
        conversation.append_message(tool_message)
        await self._conversation_repository.save(conversation)
        await self._clear_pending_tool_results(conversation.id, assistant_message_index)
        logger.warning(
            "修复未匹配的工具结果消息: conversation_id=%s, tool_count=%s, reason=%s",
            getattr(conversation, "id", None),
            len(tool_uses),
            reason,
        )
        return True

    async def _persist_pending_tool_result(
        self,
        conversation_id: str | None,
        assistant_message_index: int | None,
        result_part: ToolResultPart,
    ) -> None:
        if conversation_id is None or assistant_message_index is None:
            return
        try:
            await self._conversation_repository.save_pending_tool_result(
                conversation_id,
                assistant_message_index,
                result_part,
            )
        except AttributeError:
            return

    async def _list_pending_tool_results(
        self,
        conversation_id: str,
        assistant_message_index: int,
    ) -> list[ToolResultPart]:
        try:
            return await self._conversation_repository.list_pending_tool_results(
                conversation_id,
                assistant_message_index,
            )
        except AttributeError:
            return []

    async def _clear_pending_tool_results(
        self,
        conversation_id: str,
        assistant_message_index: int,
    ) -> None:
        try:
            await self._conversation_repository.clear_pending_tool_results(
                conversation_id,
                assistant_message_index,
            )
        except AttributeError:
            return

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
        initial_instance = self._runtime_gateway.get_agent_instance(instance.instance_id)
        if not initial_instance.pending_user_messages:
            return None
        initial_task = self._runtime_gateway.get_task(initial_instance.task_id)
        async with self._task_operation_gate.hold_key(
            self._task_operation_gate.key_for_task(initial_task),
        ):
            current_instance = self._runtime_gateway.get_agent_instance(instance.instance_id)
            if not current_instance.pending_user_messages:
                return None
            current_task = self._runtime_gateway.get_task(current_instance.task_id)
            blocked_non_scene_terminal = current_task.is_terminal and current_task.scene_id is None
            if blocked_non_scene_terminal:
                next_request = None
                next_context = communication_context
                next_trace_path = trace_path
            else:
                if current_task.is_terminal:
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
        if blocked_non_scene_terminal:
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
        assert next_request is not None
        next_conversation = await self._load_or_create_conversation(current_instance, next_request)
        await self._repair_unmatched_tool_results(
            next_conversation,
            reason="previous_execution_interrupted",
        )
        next_context = next_context.with_metadata(conversation_id=next_conversation.id)
        await self._publish_task_status(
            next_context,
            "agent.task.started",
            task_id=current_task.task_id,
            instance_id=current_instance.instance_id,
        )
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

    async def _pause_for_user_interrupt(
        self,
        *,
        task: AgentTask,
        instance: AgentInstance,
        conversation_id: str,
        trace_path: Any,
        communication_context: RuntimeCommunicationContext,
        interrupt_state: _AgentExecutionInterruptState,
        assistant_message: UniMessage | None,
    ) -> AgentExecutionResult:
        self._runtime_gateway.pause_agent_instance(instance.instance_id)
        self._runtime_gateway.pause_task(task.task_id)
        self._trace_recorder.record_event(
            trace_path,
            "instance_paused_by_user_interrupt",
            {
                "reason": interrupt_state.reason,
                "current_llm_request": interrupt_state.current_llm_request,
            },
        )
        await self._event_publisher.publish(
            communication_context,
            "agent.task.paused",
            {
                "task_id": task.task_id,
                "agent_instance_id": instance.instance_id,
                "reason": "user_interrupted",
                "interrupt_reason": interrupt_state.reason,
                "current_llm_request": interrupt_state.current_llm_request,
            },
            source="agent_execution_engine",
        )
        return AgentExecutionResult(
            task_id=task.task_id,
            instance_id=instance.instance_id,
            conversation_id=conversation_id,
            trace_file_path=str(trace_path),
            assistant_message=assistant_message,
            communication_stream_id=communication_context.stream_id,
        )

    def _load_agent_definition(self, agent_definition_id: str) -> AgentDefinition:
        """加载并校验 Agent 静态定义。"""
        definition = self._agent_definition_gateway.get_agent_definition(agent_definition_id)
        if definition.disabled:
            raise ValueError(f"Agent 定义已禁用，不能执行: {agent_definition_id}")
        return definition

    def _resolve_instance_llm_policy(
        self,
        instance: AgentInstance,
    ) -> tuple[tuple[ResolvedLlmProviderModel, ...], str]:
        if instance.llm_policy_group_id:
            policy = self._llm_registry_gateway.resolve_execution_policy(instance.llm_policy_group_id)
            candidates = tuple(candidate.resolved_model for candidate in policy.candidates)
            if not candidates:
                raise ValueError(f"LLM 执行策略组没有可用候选项: {instance.llm_policy_group_id}")
            return candidates, _normalize_unsupported_input_modality_strategy(
                policy.unsupported_input_modality_strategy
            )
        return (
            (
                self._llm_registry_gateway.resolve_provider_model(
                    instance.llm_provider_id,
                    instance.llm_model_binding_id,
                ),
            ),
            UNSUPPORTED_INPUT_MODALITY_STRATEGY_DEGRADE,
        )

    async def _send_prompt_with_policy_failover(
        self,
        *,
        task: AgentTask,
        trace_path: Any,
        conversation: Any,
        agent_definition: AgentDefinition,
        instance: AgentInstance,
        resolved_candidates: tuple[ResolvedLlmProviderModel, ...],
        unsupported_modality_strategy: str,
        render_options: dict[str, Any],
        request_overrides: dict[str, Any],
        communication_context: RuntimeCommunicationContext,
        interrupt_state: _AgentExecutionInterruptState,
        execution_frame_id: str | None,
    ) -> _PromptWithConversationResult:
        last_error: Exception | None = None
        skipped_for_modalities = False
        llm_call_id = f"llm-call-{uuid.uuid4().hex}"

        def _observe_llm_request_attempt(payload: dict[str, Any]) -> None:
            request_payload = dict(payload)
            interrupt_state.current_llm_request = request_payload
            self._record_execution_llm_attempt(
                instance_id=instance.instance_id,
                frame_id=execution_frame_id,
                attempt=request_payload,
            )

        self._update_execution_frame_metadata(
            instance_id=instance.instance_id,
            frame_id=execution_frame_id,
            updates={"llm_call_id": llm_call_id, "active_conversation_id": conversation.id},
        )
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
                self._record_execution_loop_step(
                    instance_id=instance.instance_id,
                    stage="llm.policy.candidate.skipped",
                    status="skipped",
                    summary="LLM 执行策略候选因不支持当前输入模态被跳过。",
                    details={
                        "task_id": task.task_id,
                        "conversation_id": conversation.id,
                        "candidate_index": candidate_index,
                        "provider_id": resolved_model.provider_id,
                        "binding_id": resolved_model.binding_id,
                        "model_id": resolved_model.model_id,
                        "missing_input_modalities": missing_modalities,
                    },
                    expected_next="next_policy_candidate",
                )
                skipped_for_modalities = True
                continue
            def _record_prompt_preparation_step(
                stage: str,
                status: str,
                summary: str,
                details: dict[str, Any] | None = None,
                expected_next: str | None = None,
            ) -> None:
                step_details = {
                    "task_id": task.task_id,
                    "conversation_id": conversation.id,
                    "candidate_index": candidate_index,
                    "provider_id": resolved_model.provider_id,
                    "binding_id": resolved_model.binding_id,
                    "model_id": resolved_model.model_id,
                    "api_format": resolved_model.api_format,
                }
                if details:
                    step_details.update(details)
                self._record_execution_loop_step(
                    instance_id=instance.instance_id,
                    stage=stage,
                    status=status,
                    summary=summary,
                    details=step_details,
                    expected_next=expected_next,
                )

            if instance.execution_substate == EXECUTION_SUBSTATE_NOTE_UPDATE:
                tools = self._task_note_prompt_provider.get_note_tools(resolved_model.api_format)
                note_tool_names = self._task_note_prompt_provider.get_note_tool_names()
                system_prompt = self._task_note_prompt_provider.render_note_system_prompt(
                    agent_instance_id=instance.instance_id,
                    agent_name=agent_definition.name,
                    current_working_directory=instance.current_working_directory,
                    available_tools=note_tool_names,
                )
                _record_prompt_preparation_step(
                    "llm.prompt.note_update.prepared",
                    "completed",
                    "Note 更新状态：已构建 32 个 Note 专属工具并渲染 Note 更新系统提示词。",
                    {
                        "system_prompt_length": len(system_prompt),
                        "tool_count": len(tools),
                        "tool_names": [tool.name for tool in tools],
                    },
                    "llm.candidate.request.start",
                )
            else:
                _record_prompt_preparation_step(
                    "llm.prompt.tools.build.start",
                    "started",
                    "开始按 AgentDefinition 本地工具能力边界构建 LLM 可见工具 schema。",
                    {
                        "agent_definition_id": agent_definition.agent_id,
                        "local_tool_id_count": len(agent_definition.tool_ids),
                        "bound_mcp_service_count": len(agent_definition.mcp_tools),
                        "bound_mcp_tool_count": sum(len(binding.tool_ids) for binding in agent_definition.mcp_tools),
                    },
                    "llm.prompt.tools.build.complete",
                )
                tools = await self._tool_dispatcher.build_llm_tools(
                    agent_definition=agent_definition,
                    api_format=resolved_model.api_format,
                )
                tool_source_counts: dict[str, int] = {}
                for tool in tools:
                    tool_source_counts[tool.source] = tool_source_counts.get(tool.source, 0) + 1
                _record_prompt_preparation_step(
                    "llm.prompt.tools.build.complete",
                    "completed",
                    "LLM 可见工具 schema 构建完成。",
                    {
                        "tool_schema_count": len(tools),
                        "tool_names": [tool.name for tool in tools],
                        "tool_source_counts": tool_source_counts,
                        "mcp_direct_tool_names": [tool.name for tool in tools if tool.source == "mcp"],
                    },
                    "llm.prompt.mcp_services.load.start",
                )
                _record_prompt_preparation_step(
                    "llm.prompt.mcp_services.load.start",
                    "started",
                    "开始加载 MCP 服务与工具摘要提示词。",
                    {
                        "bound_mcp_service_count": len(agent_definition.mcp_tools),
                        "bound_mcp_tool_count": sum(len(binding.tool_ids) for binding in agent_definition.mcp_tools),
                    },
                    "llm.prompt.mcp_services.load.complete",
                )
                mcp_services_prompt = await self._load_mcp_services_prompt(agent_definition)
                _record_prompt_preparation_step(
                    "llm.prompt.mcp_services.load.complete",
                    "completed",
                    "MCP 服务与工具摘要提示词加载完成。",
                    {
                        "mcp_services_prompt_present": bool(mcp_services_prompt),
                        "mcp_services_prompt_length": len(mcp_services_prompt),
                    },
                    "llm.prompt.render.start",
                )
                _record_prompt_preparation_step(
                    "llm.prompt.render.start",
                    "started",
                    "开始渲染系统提示词。",
                    {
                        "system_prompt_segment_count": len(agent_definition.system_prompt),
                        "skill_id_count": len(agent_definition.skill_ids),
                        "available_sub_agent_id_count": len(agent_definition.available_sub_agent_ids),
                        "render_option_keys": sorted(render_options.keys()),
                    },
                    "llm.prompt.agent_segments.start",
                )
                system_prompt = self._render_system_prompt(
                    agent_definition,
                    instance.instance_id,
                    instance.current_working_directory,
                    render_options,
                    tools,
                    mcp_services_prompt=mcp_services_prompt,
                    prompt_step_recorder=_record_prompt_preparation_step,
                )
                _record_prompt_preparation_step(
                    "llm.prompt.render.complete",
                    "completed",
                    "系统提示词渲染完成。",
                    {
                        "system_prompt_present": system_prompt is not None,
                        "system_prompt_length": len(system_prompt or ""),
                    },
                    "llm.prompt.prepared",
                )
            self._record_execution_loop_step(
                instance_id=instance.instance_id,
                stage="llm.prompt.prepared",
                status="prepared",
                summary="已为当前候选构建 LLM 可见工具 schema 并渲染系统提示词。",
                details={
                    "task_id": task.task_id,
                    "conversation_id": conversation.id,
                    "candidate_index": candidate_index,
                    "provider_id": resolved_model.provider_id,
                    "binding_id": resolved_model.binding_id,
                    "model_id": resolved_model.model_id,
                    "api_format": resolved_model.api_format,
                    "tool_schema_count": len(tools),
                    "tool_names": [tool.name for tool in tools],
                    "system_prompt_present": system_prompt is not None,
                    "system_prompt_length": len(system_prompt or ""),
                    "execution_substate": instance.execution_substate,
                },
                expected_next="context_compression_or_llm_request",
            )
            try:
                if instance.execution_substate != EXECUTION_SUBSTATE_NOTE_UPDATE:
                    compression_decision = self._check_compression_gate(conversation, resolved_model)
                    if compression_decision.should_compress:
                        self._record_execution_loop_step(
                            instance_id=instance.instance_id,
                            stage="context_compression.note_update.triggered",
                            status="triggered",
                            summary="上下文使用量达到阈值，触发 Note 更新状态切换与基线快照注入。",
                            details={
                                "task_id": task.task_id,
                                "conversation_id": conversation.id,
                                "decision_reason": compression_decision.reason,
                                "used_tokens": compression_decision.used_tokens,
                                "threshold": compression_decision.threshold,
                            },
                            expected_next="task_note_snapshot_build",
                        )
                        # 步骤 1：从 PG 读取 Note 基线快照
                        note_snapshot = await self._task_note_snapshot_service.build_task_note_snapshot_json(
                            task_id=task.task_id,
                            agent_instance_id=instance.instance_id,
                        )

                        # 步骤 2：追加边界 User 提示消息（包含基线 Note 快照）
                        boundary_prompt = self._task_note_prompt_provider.build_compression_boundary_prompt(note_snapshot)
                        boundary_msg = UniMessage(
                            role=ROLE_USER,
                            content=[TextPart(text=boundary_prompt)],
                            message_id=f"msg_{uuid.uuid4().hex[:12]}",
                        )
                        conversation.append_message(boundary_msg, [TextPartMeta()])

                        # 步骤 3：更新 conversation.timeline（冻结当前业务 prompt/tools 为 historical，追加 Note prompt/tools 的活跃配置事件，并保存会话）
                        self._freeze_and_append_note_timeline(
                            conversation=conversation,
                            agent_definition=agent_definition,
                            business_system_prompt=system_prompt,
                            business_tools=tools,
                            boundary_message_id=boundary_msg.message_id,
                        )
                        await self._conversation_repository.save(conversation)

                        # 步骤 4：更新实例状态为 note_update 并持久化到 SQLite
                        instance = instance.enter_note_update()
                        self._runtime_gateway.update_agent_instance_execution_substate(
                            instance.instance_id,
                            EXECUTION_SUBSTATE_NOTE_UPDATE,
                        )

                        # 步骤 5：当前轮次立即切换为 Note Prompt + Note Tools 发送给 LLM
                        tools = self._task_note_prompt_provider.get_note_tools(resolved_model.api_format)
                        note_tool_names = self._task_note_prompt_provider.get_note_tool_names()
                        system_prompt = self._task_note_prompt_provider.render_note_system_prompt(
                            agent_instance_id=instance.instance_id,
                            agent_name=agent_definition.name,
                            current_working_directory=instance.current_working_directory,
                            available_tools=note_tool_names,
                        )
                        self._trace_recorder.record_event(
                            trace_path,
                            "context_compression_note_update_entered",
                            {
                                "task_id": task.task_id,
                                "agent_instance_id": instance.instance_id,
                                "boundary_message_id": boundary_msg.message_id,
                                "decision_reason": compression_decision.reason,
                            },
                        )
                        await self._event_publisher.publish(
                            communication_context,
                            "agent.context_compression.note_update_entered",
                            {
                                "task_id": task.task_id,
                                "agent_instance_id": instance.instance_id,
                                "conversation_id": conversation.id,
                                "reason": compression_decision.reason,
                            },
                            source="agent_execution_engine",
                        )
                    if interrupt_state.requested:
                        raise LlmRequestCancelled("LLM request cancelled.")
                self._set_execution_phase(
                    task_id=task.task_id,
                    instance_id=instance.instance_id,
                    stream_id=communication_context.stream_id,
                    conversation_id=conversation.id,
                    phase="llm_prompt",
                    metadata={
                        "candidate_index": candidate_index,
                        "provider_id": resolved_model.provider_id,
                        "model_id": resolved_model.model_id,
                        "model_alias": resolved_model.model_alias,
                    },
                )
                conversation_history_copy_message_count = len(conversation.messages)
                communication_context = communication_context.with_metadata(
                    conversation_id=conversation.id,
                    conversation_history_copy_message_count=conversation_history_copy_message_count,
                )
                self._record_execution_loop_step(
                    instance_id=instance.instance_id,
                    stage="llm.candidate.request.start",
                    status="started",
                    summary="开始调用当前 LLM 候选。",
                    details={
                        "task_id": task.task_id,
                        "conversation_id": conversation.id,
                        "candidate_index": candidate_index,
                        "provider_id": resolved_model.provider_id,
                        "binding_id": resolved_model.binding_id,
                        "model_id": resolved_model.model_id,
                        "model_alias": resolved_model.model_alias,
                        "llm_call_id": llm_call_id,
                        "conversation_history_copy_message_count": conversation_history_copy_message_count,
                        "has_next_candidate": candidate_index < len(resolved_candidates) - 1,
                    },
                    expected_next="llm.candidate.response",
                )
                conversation.system_prompt = system_prompt
                conversation.tools = tuple(
                    deepcopy(t.schema) if hasattr(t, "schema") else deepcopy(t)
                    for t in tools
                )
                if (
                    instance.execution_substate != EXECUTION_SUBSTATE_NOTE_UPDATE
                    and not getattr(conversation, "timeline", None)
                ):
                    if system_prompt is not None:
                        conversation.record_configuration_event(
                            ConfigurationComponentEvent(
                                event_id=f"evt_prompt_{uuid.uuid4().hex[:8]}",
                                definition_id=agent_definition.agent_id,
                                anchor="start",
                                sequence=1,
                                status="active",
                                component="prompt",
                            )
                        )
                    if tools:
                        conversation.record_configuration_event(
                            ConfigurationComponentEvent(
                                event_id=f"evt_tool_{uuid.uuid4().hex[:8]}",
                                definition_id=agent_definition.agent_id,
                                anchor="start",
                                sequence=0,
                                status="active",
                                component="tool",
                            )
                        )
                response = await self._llm_request_service.send_prompt(
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
                        cancellation_checker=lambda state=interrupt_state: state.requested,
                        request_canceller_observer=interrupt_state.register_current_llm_request_canceller,
                        request_attempt_observer=_observe_llm_request_attempt,
                    )
                )
                self._record_execution_loop_step(
                    instance_id=instance.instance_id,
                    stage="llm.candidate.response",
                    status="succeeded",
                    summary="当前 LLM 候选请求成功返回。",
                    details={
                        "task_id": task.task_id,
                        "conversation_id": conversation.id,
                        "candidate_index": candidate_index,
                        "provider_id": resolved_model.provider_id,
                        "binding_id": resolved_model.binding_id,
                        "model_id": resolved_model.model_id,
                        "llm_call_id": llm_call_id,
                    },
                    expected_next="assistant_message.persist",
                )
                return _PromptWithConversationResult(
                    response=response,
                    conversation=conversation,
                    instance=instance,
                    conversation_history_copy_message_count=conversation_history_copy_message_count,
                )
            except LlmRequestCancelled:
                raise
            except Exception as exc:
                last_error = exc
                can_failover = (
                    candidate_index < len(resolved_candidates) - 1
                    and isinstance(exc, LlmProviderModelServiceUnavailable)
                )
                self._record_execution_loop_step(
                    instance_id=instance.instance_id,
                    stage="llm.candidate.response",
                    status="failed",
                    summary="当前 LLM 候选请求失败，Runtime 已判断是否可按策略 failover。",
                    details={
                        "task_id": task.task_id,
                        "conversation_id": conversation.id,
                        "candidate_index": candidate_index,
                        "provider_id": resolved_model.provider_id,
                        "binding_id": resolved_model.binding_id,
                        "model_id": resolved_model.model_id,
                        "error_type": exc.__class__.__name__,
                        "error_message": str(exc),
                        "will_failover": can_failover,
                    },
                    expected_next="llm.policy.failover" if can_failover else "execution_failed",
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
                    "channel": pending_message.metadata.get("channel") or pending_message.metadata.get("transport") or "web",
                    "metadata": dict(pending_message.metadata),
                    "part_count": len(user_message.content),
                    "queued_received_at": pending_message.received_at.isoformat(),
                },
                source="agent_execution_engine",
            )
        await self._conversation_repository.save(conversation)
        return len(pending_messages)

    def _check_compression_gate(
        self,
        conversation: Conversation,
        resolved_model: ResolvedLlmProviderModel,
    ) -> CompressionDecision:
        if self._context_compression_service is not None:
            try:
                config = replace(
                    self._context_compression_service._config_accessor(),
                    input_mode="native_multimodal_history",
                )
                return decide_context_compression(conversation, resolved_model, config)
            except Exception as exc:
                logger.warning("检查上下文压缩门禁异常: %s", exc)
        return CompressionDecision(False, "no_compression_service")

    def _freeze_and_append_note_timeline(
        self,
        *,
        conversation: Conversation,
        agent_definition: AgentDefinition,
        business_system_prompt: str | None,
        business_tools: tuple[LlmToolDefinition, ...] | list[LlmToolDefinition],
        boundary_message_id: str,
    ) -> None:
        business_tool_schemas = tuple(
            deepcopy(t.schema) if hasattr(t, "schema") else deepcopy(t)
            for t in business_tools
        )
        if not conversation.timeline:
            if business_system_prompt is not None:
                conversation.record_configuration_event(
                    ConfigurationComponentEvent(
                        event_id=f"evt_prompt_{uuid.uuid4().hex[:8]}",
                        definition_id=agent_definition.agent_id,
                        anchor="start",
                        sequence=1,
                        status="active",
                        component="prompt",
                    )
                )
            if business_tools:
                conversation.record_configuration_event(
                    ConfigurationComponentEvent(
                        event_id=f"evt_tool_{uuid.uuid4().hex[:8]}",
                        definition_id=agent_definition.agent_id,
                        anchor="start",
                        sequence=0,
                        status="active",
                        component="tool",
                    )
                )

        frozen_timeline: list[ConfigurationComponentEvent] = []
        for evt in conversation.timeline:
            if evt.status == "active":
                snapshot = (
                    business_system_prompt or ""
                    if evt.component == "prompt"
                    else business_tool_schemas
                )
                frozen_timeline.append(
                    replace(
                        evt,
                        status="historical",
                        snapshot=snapshot,
                    )
                )
            else:
                frozen_timeline.append(evt)
        conversation.timeline = frozen_timeline

        conversation.record_configuration_event(
            ConfigurationComponentEvent(
                event_id=f"evt_prompt_{uuid.uuid4().hex[:8]}",
                definition_id=NOTE_UPDATE_SYSTEM_PROMPT_TEMPLATE_ID,
                anchor="after_message_id",
                anchor_message_id=boundary_message_id,
                sequence=1,
                status="active",
                component="prompt",
            )
        )
        conversation.record_configuration_event(
            ConfigurationComponentEvent(
                event_id=f"evt_tool_{uuid.uuid4().hex[:8]}",
                definition_id="task_note",
                anchor="after_message_id",
                anchor_message_id=boundary_message_id,
                sequence=0,
                status="active",
                component="tool",
            )
        )

    def _render_system_prompt(
        self,
        definition: AgentDefinition,
        agent_instance_id: str,
        current_working_directory: str,
        render_options: dict[str, Any],
        llm_tools: tuple[LlmToolDefinition, ...],
        *,
        mcp_services_prompt: str | None = None,
        prompt_step_recorder: Callable[[str, str, str, dict[str, Any] | None, str | None], None] | None = None,
    ) -> str | None:
        """按运行时上下文、AgentDefinition.system_prompt 和工具提示顺序拼接最终系统提示词。"""

        def _record_prompt_step(
            stage: str,
            status: str,
            summary: str,
            details: dict[str, Any] | None = None,
            expected_next: str | None = None,
        ) -> None:
            if prompt_step_recorder is None:
                return
            prompt_step_recorder(stage, status, summary, details, expected_next)

        template_ids = tuple(
            segment.template_id for segment in definition.system_prompt if segment.template_id is not None
        )
        _record_prompt_step(
            "llm.prompt.agent_segments.start",
            "started",
            "开始读取 AgentDefinition 系统提示词片段。",
            {
                "system_prompt_segment_count": len(definition.system_prompt),
                "template_segment_count": len(template_ids),
                "template_ids": list(template_ids),
            },
            "llm.prompt.agent_segments.complete",
        )
        segments: list[str] = []
        for segment in definition.system_prompt:
            if segment.text is not None:
                segments.append(segment.text)
            elif segment.template_id is not None:
                snippet = self._prompt_snippet_gateway.get_snippet(segment.template_id)
                segments.append(snippet.text)
        text = "\n\n".join(item for item in segments if item.strip())
        _record_prompt_step(
            "llm.prompt.agent_segments.complete",
            "completed",
            "AgentDefinition 系统提示词片段读取完成。",
            {
                "rendered_segment_count": len(segments),
                "agent_prompt_text_length": len(text),
            },
            "llm.prompt.runtime_context.start",
        )

        _record_prompt_step(
            "llm.prompt.runtime_context.start",
            "started",
            "开始构造运行时上下文提示词。",
            {
                "agent_instance_id": agent_instance_id,
                "current_working_directory": current_working_directory,
            },
            "llm.prompt.runtime_context.complete",
        )
        skill_root_directory = self._resolve_skill_root_directory()
        runtime_context_prompt = _build_runtime_context_prompt(
            definition,
            agent_instance_id,
            current_working_directory,
            skill_root_directory,
        )
        _record_prompt_step(
            "llm.prompt.runtime_context.complete",
            "completed",
            "运行时上下文提示词构造完成。",
            {"runtime_context_prompt_length": len(runtime_context_prompt)},
            "llm.prompt.mcp_services.start",
        )

        _record_prompt_step(
            "llm.prompt.mcp_services.start",
            "started",
            "开始构造 MCP 服务提示词。",
            {
                "bound_mcp_service_count": len(definition.mcp_tools),
                "bound_mcp_tool_count": sum(len(binding.tool_ids) for binding in definition.mcp_tools),
                "preloaded_mcp_services_prompt_present": bool(mcp_services_prompt),
            },
            "llm.prompt.mcp_services.complete",
        )
        rendered_mcp_services_prompt = mcp_services_prompt or self._build_mcp_services_prompt(definition)
        _record_prompt_step(
            "llm.prompt.mcp_services.complete",
            "completed",
            "MCP 服务提示词构造完成。",
            {
                "mcp_services_prompt_present": bool(rendered_mcp_services_prompt),
                "mcp_services_prompt_length": len(rendered_mcp_services_prompt),
            },
            "llm.prompt.skills.start",
        )

        _record_prompt_step(
            "llm.prompt.skills.start",
            "started",
            "开始构造 Skill 提示词。",
            {"skill_id_count": len(definition.skill_ids)},
            "llm.prompt.skills.complete",
        )
        skills_prompt = self._build_skills_prompt(definition)
        _record_prompt_step(
            "llm.prompt.skills.complete",
            "completed",
            "Skill 提示词构造完成。",
            {
                "skills_prompt_present": bool(skills_prompt),
                "skills_prompt_length": len(skills_prompt),
            },
            "llm.prompt.available_subagents.start",
        )

        _record_prompt_step(
            "llm.prompt.available_subagents.start",
            "started",
            "开始构造可用子 Agent 提示词。",
            {"available_sub_agent_id_count": len(definition.available_sub_agent_ids)},
            "llm.prompt.available_subagents.complete",
        )
        available_subagents_prompt = self._build_available_subagents_prompt(definition)
        _record_prompt_step(
            "llm.prompt.available_subagents.complete",
            "completed",
            "可用子 Agent 提示词构造完成。",
            {
                "available_subagents_prompt_present": bool(available_subagents_prompt),
                "available_subagents_prompt_length": len(available_subagents_prompt),
            },
            "llm.prompt.lifecycle.start",
        )

        _record_prompt_step(
            "llm.prompt.lifecycle.start",
            "started",
            "开始构造 lifecycle runtime 提示词。",
            {"render_option_keys": sorted(render_options.keys())},
            "llm.prompt.lifecycle.complete",
        )
        lifecycle_prompt = _build_lifecycle_runtime_prompt(render_options)
        _record_prompt_step(
            "llm.prompt.lifecycle.complete",
            "completed",
            "lifecycle runtime 提示词构造完成。",
            {
                "lifecycle_prompt_present": bool(lifecycle_prompt),
                "lifecycle_prompt_length": len(lifecycle_prompt),
            },
            "llm.prompt.tool_usage_notice.start",
        )

        _record_prompt_step(
            "llm.prompt.tool_usage_notice.start",
            "started",
            "开始构造工具使用注意事项提示词。",
            {"llm_tool_names": [tool.name for tool in llm_tools]},
            "llm.prompt.tool_usage_notice.complete",
        )
        tool_usage_notice = self._build_tool_usage_notice(definition, llm_tools)
        _record_prompt_step(
            "llm.prompt.tool_usage_notice.complete",
            "completed",
            "工具使用注意事项提示词构造完成。",
            {"tool_usage_notice_length": len(tool_usage_notice)},
            "llm.prompt.assemble.complete",
        )

        skill_root_path = Path(skill_root_directory).expanduser().resolve(strict=False) if skill_root_directory else Path("")
        dedicated_skill_file = skill_root_path / definition.agent_id / "SKILL.md" if skill_root_directory else Path("")
        working_directory = Path(current_working_directory).expanduser()
        resolved_working_directory = working_directory.resolve(strict=False)
        project_memory_file = resolved_working_directory / ".spread-ai" / "memory" / definition.agent_id / "memory.md"
        git_roots = _discover_git_roots_under(resolved_working_directory)
        git_roots_prompt = "\n".join(f"- `{git_root}`" for git_root in git_roots)

        var_context = PromptVariableContext(
            agent_instance_id=agent_instance_id,
            agent_name=definition.name or definition.agent_id,
            current_working_directory=current_working_directory,
            parent_agent_instance_id=str(render_options.get("parent_agent_instance_id") or ""),
            skill_root_directory=skill_root_directory,
            dedicated_skill_file=str(dedicated_skill_file) if skill_root_directory else "",
            dedicated_memory_file=str(project_memory_file),
            available_tools=tuple(tool.name for tool in llm_tools),
            available_mcp_services=rendered_mcp_services_prompt,
            available_subagents=available_subagents_prompt,
            available_skills=skills_prompt,
            git_repo_roots=git_roots_prompt,
            frontend_channel=str(
                render_options.get("channel")
                or render_options.get("frontend_channel")
                or "unknown"
            ),
            supported_text_formats=_format_render_option_values(
                render_options.get("supported_text_formats")
                or render_options.get("text_formats")
                or ("html",)
            ),
            preferred_text_format=str(
                render_options.get("preferred_text_format")
                or render_options.get("text_format")
                or "html"
            ),
        )
        variable_values = build_prompt_variable_values(var_context)

        # 若模板中已显式包含特定占位符，则避免在末尾重复追加该块
        append_mcp = bool(rendered_mcp_services_prompt and "{{available_mcp_services}}" not in text)
        append_skills = bool(skills_prompt and "{{available_skills}}" not in text)
        append_subagents = bool(available_subagents_prompt and "{{available_subagents}}" not in text)
        append_lifecycle = bool(
            lifecycle_prompt
            and "Runtime lifecycle tool policy" not in text
            and "生命周期工具" not in text
        )

        assembled_parts = [runtime_context_prompt, text]
        if append_mcp:
            assembled_parts.append(rendered_mcp_services_prompt)
        if append_skills:
            assembled_parts.append(skills_prompt)
        if append_subagents:
            assembled_parts.append(available_subagents_prompt)
        if append_lifecycle:
            assembled_parts.append(lifecycle_prompt)
        assembled_parts.append(tool_usage_notice)

        raw_system_prompt = "\n\n".join(item for item in assembled_parts if item.strip())
        system_prompt = render_prompt_variables(raw_system_prompt, variable_values) or None
        _record_prompt_step(
            "llm.prompt.assemble.complete",
            "completed",
            "系统提示词各片段拼接与占位符变量替换完成。",
            {
                "system_prompt_present": system_prompt is not None,
                "system_prompt_length": len(system_prompt or ""),
            },
            "llm.prompt.render.complete",
        )
        return system_prompt

    async def _load_mcp_services_prompt(self, definition: AgentDefinition) -> str:
        """按 AgentDefinition 的 MCP 绑定加载服务与工具摘要提示词。"""
        if not definition.mcp_tools or self._mcp_client_gateway is None:
            return ""
        service_ids = _mcp_service_ids(definition)
        services = {
            item.service_id: item
            for item in self._mcp_client_gateway.list_services()
        }
        available_services = [services[sid] for sid in service_ids if sid in services]
        if not available_services:
            return ""

        tools_by_service: dict[str, list[tuple[str, str | None]]] = {sid: [] for sid in service_ids}
        for binding in definition.mcp_tools:
            try:
                tool_summaries = await self._mcp_client_gateway.list_tools(binding.service_id)
            except Exception as exc:  # noqa: BLE001 - MCP 摘要不可用不应阻断 Agent 执行
                logger.warning(
                    "MCP 服务工具摘要加载失败，已退化为 AgentDefinition 工具 ID: service_id=%s, error_type=%s, error=%s",
                    binding.service_id,
                    exc.__class__.__name__,
                    exc,
                )
                tools_by_service[binding.service_id].extend((tool_id, None) for tool_id in binding.tool_ids)
                continue
            summary_map = {item.name: item for item in tool_summaries}
            for tool_id in binding.tool_ids:
                summary = summary_map.get(tool_id)
                tools_by_service[binding.service_id].append((
                    tool_id,
                    summary.description if summary is not None else None,
                ))
        return self._build_mcp_services_prompt(definition, tools_by_service=tools_by_service)

    def _build_mcp_services_prompt(
        self,
        definition: AgentDefinition,
        *,
        tools_by_service: dict[str, list[tuple[str, str | None]]] | None = None,
    ) -> str:
        if not definition.mcp_tools or self._mcp_client_gateway is None:
            return ""
        service_ids = _mcp_service_ids(definition)
        services = {
            item.service_id: item
            for item in self._mcp_client_gateway.list_services()
        }
        available_services = [services[sid] for sid in service_ids if sid in services]
        if not available_services:
            return ""

        if tools_by_service is None:
            tools_by_service = {
                binding.service_id: [(tool_id, None) for tool_id in binding.tool_ids]
                for binding in definition.mcp_tools
            }

        # system prompt 仅注入 MCP 服务和可用工具摘要；具体参数 schema 通过 MCP 元工具按需读取。
        lines = ["# 可用的 MCP 服务列表"]
        for service in available_services:
            description = service.description or ""
            if description:
                lines.append(f"- `{service.service_id}`：{description}")
            else:
                lines.append(f"- `{service.service_id}`")
            service_tools = tools_by_service.get(service.service_id, [])
            if not service_tools:
                continue
            lines.append("  - 可用工具：")
            for tool_name, tool_description in service_tools:
                if tool_description:
                    lines.append(f"    - `{tool_name}`：{tool_description}")
                else:
                    lines.append(f"    - `{tool_name}`")
        return "\n".join(lines)


    def _tool_schema_description(self, schema: dict[str, Any]) -> str:
        """从已转换的 LLM 工具 schema 中提取工具描述。"""
        description = schema.get("description")
        if isinstance(description, str):
            return description
        function = schema.get("function")
        if isinstance(function, dict):
            function_description = function.get("description")
            if isinstance(function_description, str):
                return function_description
        return ""

    def _build_available_subagents_prompt(self, definition: AgentDefinition) -> str:
        """构造当前 Agent 可创建子 Agent 的系统提示词摘要。"""
        if not definition.available_sub_agent_ids:
            return ""

        available_definitions: list[AgentDefinition] = []
        for sub_agent_id in definition.available_sub_agent_ids:
            try:
                sub_agent_definition = self._agent_definition_gateway.get_agent_definition(sub_agent_id)
            except KeyError:
                logger.warning(
                    "Agent 定义声明的可用子 Agent 不存在，已跳过提示词注入: parent=%s, child=%s",
                    definition.agent_id,
                    sub_agent_id,
                )
                continue
            if sub_agent_definition.disabled:
                continue
            available_definitions.append(sub_agent_definition)

        if not available_definitions:
            return ""

        lines = ["# 可用子 Agent"]
        for sub_agent_definition in available_definitions:
            name = (sub_agent_definition.name or "").strip()
            description = (sub_agent_definition.description or "").strip()
            function_desc = description or name or "未填写用途说明。"
            lines.append(f"- agent定义id：`{sub_agent_definition.agent_id}`；功能：{function_desc}")
        return "\n".join(lines)

    def _resolve_skill_root_directory(self) -> str:
        skill_gateway = getattr(self, "_skill_gateway", None)
        if skill_gateway is None:
            return ""
        try:
            settings = skill_gateway.get_settings()
        except Exception as exc:  # noqa: BLE001 - Skill 设置不可用不应阻断 Agent 执行
            logger.warning(
                "Skill 根目录读取失败，专属 Skill 路径提示将使用空根目录: error_type=%s, error=%s",
                exc.__class__.__name__,
                exc,
            )
            return ""
        return str(getattr(settings, "skills_dir", "") or "").strip()

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
        return "\n".join(lines)

    def _build_tool_usage_notice(
        self,
        definition: AgentDefinition,
        llm_tools: tuple[LlmToolDefinition, ...],
    ) -> str:
        """构造面向本轮 LLM 可见工具的统一工具使用注意事项。"""
        llm_tool_names = {tool.name for tool in llm_tools}
        tools = self._local_tool_gateway.list_tools(public_only=False, group_by=False)
        lifecycle_tool_names = [
            item.tool_name
            for item in tools
            if item.category == "lifecycle"
            and item.tool_name in llm_tool_names
            and (is_tool_default_enabled_category(item.category) or item.tool_name in definition.tool_ids)
        ]

        lines = [
            "# 工具调用注意事项",
            "- 禁止调用模型内置网络搜索和图片生成工具。",
        ]
        if "generate_image" in llm_tool_names:
            lines.append("- 图片生成请使用generate_image工具。")
        if {"web_search", "read_url_context"} & llm_tool_names:
            lines.append("- 网络搜索请使用web_search，read_url_context工具。")
        if lifecycle_tool_names:
            names = "，".join(lifecycle_tool_names)
            lines.append(
                f"- 禁止并发调用“{names}”这些工具，这些工具只能单独调用！这些工具只能单独调用！这些工具只能单独调用！"
            )
        return "\n".join(lines)

    async def _execute_single_tool_use(
        self,
        definition: AgentDefinition,
        tool_use: ToolUsePart,
        communication_context: RuntimeCommunicationContext,
        *,
        instance: AgentInstance | None = None,
    ) -> ToolDispatchResult:
        """执行单个工具调用，调度层异常也转换为 LLM 可见的工具错误结果。"""
        try:
            if instance is not None and instance.execution_substate == EXECUTION_SUBSTATE_NOTE_UPDATE:
                note_tool_names = self._task_note_prompt_provider.get_note_tool_names()
                if tool_use.name not in note_tool_names:
                    raise PermissionError(f"Note 更新状态下仅允许调用 Task Note 工具，禁止调用工具: {tool_use.name}")
                effective_definition = replace(
                    definition,
                    tool_ids=note_tool_names,
                    mcp_tools=(),
                )
            else:
                effective_definition = definition

            return await self._tool_dispatcher.dispatch_tool_call(
                agent_definition=effective_definition,
                tool_use=tool_use,
                communication_context=communication_context.with_actor(
                    actor_type="tool_dispatcher",
                    actor_id=tool_use.name,
                ),
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if _is_process_control_exception(exc):
                raise
            tool_call_id = _tool_use_ref(tool_use)
            error_message = str(exc) or exc.__class__.__name__
            logger.exception(
                "Agent 工具调用异常，已转换为工具错误结果: tool=%s, error_type=%s, error_message=%s",
                tool_use.name,
                exc.__class__.__name__,
                error_message,
            )
            tool_result = build_tool_error_result(
                error_code=exc.__class__.__name__,
                error_message=error_message,
            )
            return ToolDispatchResult(
                tool_name=tool_use.name,
                source="tool_dispatcher",
                result_part=ToolResultPart(
                    tool_use_ref=tool_call_id,
                    is_error=True,
                    output=normalize_tool_result_output(f"工具执行失败：{tool_result.error_message or 'Tool dispatch failed.'}"),
                ),
                tool_result=tool_result,
            )

    async def _execute_tool_uses(
        self,
        definition: AgentDefinition,
        tool_uses: list[ToolUsePart],
        communication_context: RuntimeCommunicationContext,
        *,
        conversation_id: str | None = None,
        assistant_message_index: int | None = None,
        interrupt_state: _AgentExecutionInterruptState,
        instance: AgentInstance | None = None,
    ) -> tuple[UniMessage, list[BaseToolResult]]:
        """执行 assistant 消息中的全部工具调用并生成 tool 消息。"""
        started_at = time.perf_counter()
        logger.info(
            "Agent 工具批执行开始: tool_count=%s, tool_names=%s",
            len(tool_uses),
            [tool_use.name for tool_use in tool_uses],
        )
        result_parts = []
        results_by_ref: dict[str, ToolDispatchResult] = {}
        tasks: list[tuple[ToolUsePart, asyncio.Task[ToolDispatchResult]]] = []
        instance_id = str(communication_context.metadata.get("agent_instance_id") or "")
        if instance_id:
            self._record_execution_loop_step(
                instance_id=instance_id,
                stage="tool_batch.dispatch_plan",
                status="started",
                summary="工具批执行函数已开始调度工具调用。",
                details={
                    "conversation_id": conversation_id,
                    "assistant_message_index": assistant_message_index,
                    "tool_count": len(tool_uses),
                    "tool_call_ids": [_tool_use_ref(tool_use) for tool_use in tool_uses],
                    "tool_names": [tool_use.name for tool_use in tool_uses],
                },
                expected_next="tool.dispatch.scheduled",
            )
        running_tools: list[dict[str, Any]] = []
        for tool_use in tool_uses:
            if interrupt_state.requested:
                cancelled_result = await self._build_cancelled_tool_dispatch_result(
                    tool_use,
                    communication_context,
                    interrupt_state=interrupt_state,
                    phase="not_started",
                )
                results_by_ref[_tool_use_ref(tool_use)] = cancelled_result
                await self._persist_pending_tool_result(
                    conversation_id,
                    assistant_message_index,
                    cancelled_result.result_part,
                )
                if instance_id:
                    self._record_execution_loop_step(
                        instance_id=instance_id,
                        stage="tool.pending_result.saved",
                        status="cancelled",
                        summary="用户中断导致工具未启动，取消结果已作为 pending tool result 持久化。",
                        details={
                            "conversation_id": conversation_id,
                            "assistant_message_index": assistant_message_index,
                            "tool_call_id": _tool_use_ref(tool_use),
                            "tool_name": tool_use.name,
                            "phase": "not_started",
                        },
                        expected_next="tool_batch.continue_dispatch",
                    )
                continue
            running_tools.append({
                "tool_call_id": _tool_use_ref(tool_use),
                "tool_name": tool_use.name,
                "source": _tool_source(tool_use),
                "started_at": _datetime_from_timestamp(time.time()),
            })
            if instance_id:
                self._set_execution_running_tools(instance_id, running_tools)
                self._record_execution_loop_step(
                    instance_id=instance_id,
                    stage="tool.dispatch.scheduled",
                    status="scheduled",
                    summary="单个工具调用已创建 asyncio task 并加入 running_tools。",
                    details={
                        "conversation_id": conversation_id,
                        "assistant_message_index": assistant_message_index,
                        "tool_call_id": _tool_use_ref(tool_use),
                        "tool_name": tool_use.name,
                        "source": _tool_source(tool_use),
                        "running_tool_count": len(running_tools),
                    },
                    expected_next="tool.dispatch.completed",
                )
            tool_task = asyncio.create_task(self._execute_single_tool_use(
                definition,
                tool_use,
                communication_context,
                instance=instance,
            ))
            tasks.append((tool_use, tool_task))
        if tasks:
            try:
                task_by_asyncio_task = {
                    task: tool_use
                    for tool_use, task in tasks
                }
                pending_tasks = set(task_by_asyncio_task)
                while pending_tasks:
                    done_tasks, pending_tasks = await asyncio.wait(
                        pending_tasks,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in done_tasks:
                        tool_use = task_by_asyncio_task[task]
                        try:
                            result = task.result()
                        except asyncio.CancelledError:
                            raise
                        except BaseException as exc:
                            if _is_process_control_exception(exc):
                                raise
                            result = self._build_exception_tool_dispatch_result(tool_use, exc)
                        results_by_ref[result.result_part.tool_use_ref] = result
                        await self._persist_pending_tool_result(
                            conversation_id,
                            assistant_message_index,
                            result.result_part,
                        )
                        if instance_id:
                            self._record_execution_loop_step(
                                instance_id=instance_id,
                                stage="tool.pending_result.saved",
                                status="saved",
                                summary="单个工具调用完成后，其 ToolResultPart 已先作为 pending tool result 持久化。",
                                details={
                                    "conversation_id": conversation_id,
                                    "assistant_message_index": assistant_message_index,
                                    "tool_call_id": result.result_part.tool_use_ref,
                                    "tool_name": result.tool_name,
                                    "source": result.source,
                                    "success": result.tool_result.success,
                                    "execution_status": result.tool_result.execution_status,
                                    "loop_decision": result.tool_result.loop_decision,
                                    "is_error": result.result_part.is_error,
                                },
                                expected_next="tool_batch.await_remaining_or_aggregate",
                            )
                        if instance_id:
                            tool_use_ref = _tool_use_ref(tool_use)
                            running_tools = [
                                item
                                for item in running_tools
                                if item.get("tool_call_id") != tool_use_ref
                            ]
                            self._set_execution_running_tools(instance_id, running_tools)
            finally:
                for _tool_use, task in tasks:
                    if not task.done():
                        task.cancel()
                if instance_id:
                    self._set_execution_running_tools(instance_id, [])
                    self._record_execution_loop_step(
                        instance_id=instance_id,
                        stage="tool_batch.running_tools.cleared",
                        status="cleared",
                        summary="工具批执行 finally 已清空运行中工具快照。",
                        details={
                            "conversation_id": conversation_id,
                            "assistant_message_index": assistant_message_index,
                            "task_count": len(tasks),
                        },
                        expected_next="tool_results.aggregate",
                    )
        results = [results_by_ref[_tool_use_ref(tool_use)] for tool_use in tool_uses]
        tool_results = []
        for result in results:
            result_parts.append(result.result_part)
            tool_results.append(result.tool_result)
            await self._publish_tool_frontend_message(result.tool_result, result.result_part.tool_use_ref, communication_context)
        if instance_id:
            self._record_execution_loop_step(
                instance_id=instance_id,
                stage="tool_results.aggregate",
                status="ready",
                summary="工具批执行结果已按 assistant 工具调用顺序聚合为 ROLE_TOOL 消息内容。",
                details={
                    "conversation_id": conversation_id,
                    "assistant_message_index": assistant_message_index,
                    "tool_count": len(tool_results),
                    "tool_result_refs": [part.tool_use_ref for part in result_parts],
                    "loop_decisions": [result.loop_decision for result in tool_results],
                    "frontend_message_count": sum(1 for result in tool_results if result.frontend_message is not None),
                },
                expected_next="return_to_execution_loop",
            )
        logger.info(
            "Agent 工具批执行完成: tool_count=%s, loop_decisions=%s, 耗时=%sms",
            len(tool_results),
            [result.loop_decision for result in tool_results],
            int((time.perf_counter() - started_at) * 1000),
        )
        return UniMessage(role=ROLE_TOOL, content=result_parts), tool_results

    def _build_exception_tool_dispatch_result(
        self,
        tool_use: ToolUsePart,
        exc: BaseException,
    ) -> ToolDispatchResult:
        tool_call_id = _tool_use_ref(tool_use)
        error_message = str(exc) or exc.__class__.__name__
        tool_result = build_tool_error_result(
            error_code=exc.__class__.__name__,
            error_message=error_message,
        )
        return ToolDispatchResult(
            tool_name=tool_use.name,
            source="tool_dispatcher",
            result_part=ToolResultPart(
                tool_use_ref=tool_call_id,
                is_error=True,
                output=normalize_tool_result_output(f"工具执行失败：{tool_result.error_message or 'Tool dispatch failed.'}"),
            ),
            tool_result=tool_result,
        )

    async def _build_interrupted_tool_results(
        self,
        tool_uses: list[ToolUsePart],
        communication_context: RuntimeCommunicationContext,
        *,
        interrupt_state: _AgentExecutionInterruptState,
        phase: str,
    ) -> tuple[UniMessage, list[BaseToolResult]]:
        results = [
            await self._build_cancelled_tool_dispatch_result(
                tool_use,
                communication_context,
                interrupt_state=interrupt_state,
                phase=phase,
            )
            for tool_use in tool_uses
        ]
        return (
            UniMessage(role=ROLE_TOOL, content=[result.result_part for result in results]),
            [result.tool_result for result in results],
        )

    async def _build_cancelled_tool_dispatch_result(
        self,
        tool_use: ToolUsePart,
        communication_context: RuntimeCommunicationContext,
        *,
        interrupt_state: _AgentExecutionInterruptState,
        phase: str,
    ) -> ToolDispatchResult:
        tool_call_id = _tool_use_ref(tool_use)
        tool_result = build_tool_error_result(
            error_code="UserInterrupted",
            error_message="Tool execution cancelled by user interruption.",
        )
        await self._event_publisher.publish(
            communication_context,
            "tool.call.cancelled",
            {
                "tool_call_id": tool_call_id,
                "tool_name": tool_use.name,
                "source": _tool_source(tool_use),
                "tool_source": _tool_source(tool_use),
                "cancelled_by": "user",
                "phase": phase,
                "reason": interrupt_state.reason,
                "result_preview": {
                    "error_message": tool_result.error_message or "Tool execution cancelled.",
                },
            },
            source="agent_execution_engine",
            correlation_id=tool_call_id,
        )
        return ToolDispatchResult(
            tool_name=tool_use.name,
            source=_tool_source(tool_use),
            result_part=ToolResultPart(
                tool_use_ref=tool_call_id,
                is_error=True,
                output=normalize_tool_result_output(f"工具执行失败：{tool_result.error_message or 'Tool execution cancelled.'}"),
            ),
            tool_result=tool_result,
        )

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
                    "requires_user_response": True,
                    "interaction_state": "pending",
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
                "requires_user_response": False,
                "interaction_state": "none",
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
        request: AgentExecutionRequest,
    ) -> None:
        """根据统一工具结果中的 loop_decision 更新运行态。"""
        details = [result.to_serializable_dict() for result in tool_results]
        if loop_decision == "pause":
            timeout_seconds, wake_mode = self._pause_wake_metadata_from_tool_results(tool_results)
            current_instance = self._runtime_gateway.pause_agent_instance(instance_id)
            self._runtime_gateway.pause_task(task_id)
            conversation_id = (
                current_instance.conversation_id if current_instance is not None else None
            ) or communication_context.metadata.get("conversation_id")
            timeout_handle = None
            if timeout_seconds > 0:
                timeout_handle = self._schedule_pause_timeout(
                    task_id=task_id,
                    instance_id=instance_id,
                    conversation_id=str(conversation_id) if conversation_id else None,
                    timeout_seconds=timeout_seconds,
                    request=request,
                    communication_context=communication_context,
                )
            pause_payload = {
                "wake_mode": wake_mode,
                "timeout_seconds": timeout_seconds,
                "wake_deadline": timeout_handle.deadline.isoformat() if timeout_handle is not None else None,
                "pause_token": timeout_handle.token if timeout_handle is not None else None,
            }
            self._trace_recorder.record_event(
                trace_path,
                "instance_paused",
                {
                    "tool_results": details,
                    **pause_payload,
                },
            )
            await self._publish_loop_decision_event(
                communication_context,
                "agent.task.paused",
                task_id=task_id,
                instance_id=instance_id,
                loop_decision=loop_decision,
                tool_results=tool_results,
                extra_payload=pause_payload,
            )
            return
        if loop_decision == "complete":
            task = self._runtime_gateway.get_task(task_id)
            if task.root_agent_instance_id != instance_id:
                relation = task.child_relation_of(instance_id)
                released_agent_instance_ids: list[str] = []
                if relation is not None and not relation.is_released:
                    _updated_task, released_instances = self._runtime_gateway.release_child_agent_instance(
                        relation.parent_instance_id,
                        instance_id,
                        reason="child_agent_completed",
                    )
                    released_agent_instance_ids = [instance.instance_id for instance in released_instances]
                else:
                    self._runtime_gateway.pause_agent_instance(instance_id)
                self._trace_recorder.record_completed(trace_path)
                if self._task_note_snapshot_service is not None:
                    targets = set(released_agent_instance_ids) | {instance_id}
                    for tid in targets:
                        try:
                            await self._task_note_snapshot_service.async_cleanup_agent_instance_note(
                                task_id=task_id, agent_instance_id=tid
                            )
                        except Exception as exc:
                            logger.warning("清理子 Agent Note 记录异常 (已忽略): %s", exc)
                await self._publish_loop_decision_event(
                    communication_context,
                    "agent.subagent.completed",
                    task_id=task_id,
                    instance_id=instance_id,
                    loop_decision=loop_decision,
                    tool_results=tool_results,
                    extra_payload={"released_agent_instance_ids": released_agent_instance_ids},
                )
                return
            await self._task_completion_coordinator.complete_root_from_lifecycle(
                task_id=task_id,
                root_instance_id=instance_id,
                trace_path=trace_path,
                communication_context=communication_context,
                tool_results=tool_results,
                completion_summary=_completion_summary_from_tool_results(tool_results),
            )
            return
        if loop_decision == "fail":
            failure_message = _failure_message_from_tool_results(tool_results)
            task = self._runtime_gateway.get_task(task_id)
            if task.root_agent_instance_id != instance_id:
                relation = task.child_relation_of(instance_id)
                released_agent_instance_ids: list[str] = []
                if relation is not None and not relation.is_released:
                    _updated_task, released_instances = self._runtime_gateway.release_child_agent_instance(
                        relation.parent_instance_id,
                        instance_id,
                        reason=failure_message,
                    )
                    released_agent_instance_ids = [instance.instance_id for instance in released_instances]
                else:
                    self._runtime_gateway.fail_agent_instance(instance_id, failure_message)
                self._trace_recorder.record_failure(trace_path, failure_message)
                if self._task_note_snapshot_service is not None:
                    targets = set(released_agent_instance_ids) | {instance_id}
                    for tid in targets:
                        try:
                            await self._task_note_snapshot_service.async_cleanup_agent_instance_note(
                                task_id=task_id, agent_instance_id=tid
                            )
                        except Exception as exc:
                            logger.warning("清理失败子 Agent Note 记录异常 (已忽略): %s", exc)
                await self._publish_loop_decision_event(
                    communication_context,
                    "agent.subagent.failed",
                    task_id=task_id,
                    instance_id=instance_id,
                    loop_decision=loop_decision,
                    tool_results=tool_results,
                    failure_message=failure_message,
                    extra_payload={"released_agent_instance_ids": released_agent_instance_ids},
                )
                return
            await self._delete_task_terminals(task_id)
            if self._task_note_snapshot_service is not None:
                try:
                    await self._task_note_snapshot_service.async_cleanup_task_note(task_id=task_id)
                except Exception as exc:
                    logger.warning("失败清理 Task Note 记录异常 (已忽略): %s", exc)
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

    async def _delete_task_terminals(self, task_id: str) -> None:
        try:
            from agent_runtime.terminal.terminal_factory import TerminalFactory

            deleted = await TerminalFactory.create().delete_task_terminals(task_id)
            if deleted:
                logger.info("Agent 任务终态清理终端完成: task_id=%s terminal_count=%s", task_id, len(deleted))
        except Exception:
            logger.exception("Agent 任务终态清理终端失败: task_id=%s", task_id)

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
        extra_payload: dict[str, Any] | None = None,
    ) -> None:
        """发布工具结果驱动的 Agent 循环决策事件。"""
        frontend_messages = [
            result.frontend_message.model_dump(mode="json")
            for result in tool_results
            if result.frontend_message is not None
        ]
        payload = {
            "task_id": task_id,
            "agent_instance_id": instance_id,
            "loop_decision": loop_decision,
            "tool_result_count": len(tool_results),
            "frontend_messages": frontend_messages,
            "failure_message": failure_message,
        }
        if extra_payload:
            payload.update(extra_payload)
        await self._event_publisher.publish(
            communication_context,
            event_type,
            payload,
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
        "recorded_message_count": len(getattr(conversation, "messages", [])),
    }


def _extract_tool_uses(message: UniMessage) -> list[ToolUsePart]:
    """从 assistant 消息中提取工具调用。"""
    return [part for part in message.content if isinstance(part, ToolUsePart)]


def _tool_use_ref(tool_use: ToolUsePart) -> str:
    """确定工具结果要关联的工具调用引用。"""
    return tool_use.tool_use_id or tool_use.name


def _tool_source(tool_use: ToolUsePart) -> str:
    return "mcp" if tool_use.name.startswith("mcp__") else "local"


def _is_process_control_exception(exc: BaseException) -> bool:
    return isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit))


def _normalize_unsupported_input_modality_strategy(strategy: str | None) -> str:
    if strategy == UNSUPPORTED_INPUT_MODALITY_STRATEGY_SKIP_CANDIDATE:
        return strategy
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


def _mcp_service_ids(definition: AgentDefinition) -> list[str]:
    service_ids: list[str] = []
    for binding in definition.mcp_tools:
        if binding.service_id not in service_ids:
            service_ids.append(binding.service_id)
    return service_ids


def _build_runtime_context_prompt(
    definition: AgentDefinition,
    agent_instance_id: str,
    current_working_directory: str,
    skill_root_directory: str,
) -> str:
    working_directory = Path(current_working_directory).expanduser()
    resolved_working_directory = working_directory.resolve(strict=False)
    git_roots = _discover_git_roots_under(resolved_working_directory)
    git_roots_lines = "\n".join(f"- `{git_root}`" for git_root in git_roots)
    git_roots_section = (
        f"\n当前工作目录下的 Git 仓库根目录：\n\n{git_roots_lines}\n"
        if git_roots_lines
        else ""
    )
    return textwrap.dedent(
        f"""\
# 身份id和工作空间

你自己的 Agent 实例 ID：`{agent_instance_id}`
当前工作目录：`{resolved_working_directory}`{git_roots_section}
# 系统环境

- Current working directory: `{resolved_working_directory}`
- Operating system: `{platform.system()}`
- OS release: `{platform.release()}`
- OS version: `{platform.version()}`
- Machine: `{platform.machine()}`
"""
    ).strip()


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
        or ("html",)
    )
    preferred_format = str(
        render_options.get("preferred_text_format")
        or render_options.get("text_format")
        or "html"
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
        "- `pause_task` tool: pause execution without sending any message and wait for external "
        "events (other Agent messages or system events). A positive `timeout_seconds` is a finite "
        "checkpoint: whichever arrives first — the external event or timeout expiry — resumes the "
        "next LLM loop. When waiting for a known delegated result from a local child Agent, remote "
        "Agent, or definite async job, choose a positive checkpoint based on expected complexity "
        "(often roughly 5–50 minutes). Do not use `-1` for that wait or assume the completion "
        "notification is perfectly reliable. If the checkpoint expires without a result, inspect "
        "the child/remote Agent or async-job status; if it is paused or silent, proactively ask "
        "whether it is complete and request progress, blockers, and ETA to avoid a stuck system. "
        "Use `timeout_seconds=-1` only when this Agent's assigned work is fully complete, the final "
        "result has already been reported to a parent or remote upstream, and no known subtask or "
        "result remains outstanding; this is indefinite dormant waiting for uncertain future "
        "instructions. Do not use positive timeouts to create meaningless periodic wake loops. "
        "For waiting on real user replies use `message_user(action=request_response)`, not "
        "`pause_task`.\n"
        "- `end_task` tool: end task session for SUCCESSFUL completion. Required sequence: "
        "first call `message_user(action=request_response)` to report results and wait for "
        "user verification; only after explicit user approval should you call `end_task`. "
        "For task failure use `fail_task` instead.\n"
        "- `fail_task` tool: declare unrecoverable failure and end the loop with a fail decision. Use only when "
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
        "- Set `message_user.text_format` explicitly to `html`; `message_user.message` must be an HTML fragment following the tool schema, not Markdown.\n"
        "- For readable HTML, follow `message_user` schema guidance: prefer tables for comparison/range/status/plan/result information, and keep paragraph/list hierarchy to at most three levels (plain heading/paragraph, bullet main items, dash details) without forcing all three levels.\n"
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


def _completion_summary_from_tool_results(tool_results: list[BaseToolResult]) -> str:
    """从 complete 工具结果中提取审计摘要。"""
    for result in tool_results:
        if result.loop_decision == "complete" and isinstance(result.output, str) and result.output.strip():
            return result.output.strip()
    return "Agent lifecycle tool requested task completion."


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
    progress_callback: Callable[[int], None] | None = None,
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
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            if progress_callback is not None:
                progress_callback(elapsed_ms)
            logger.warning(
                "Agent 阶段仍在执行: stage=%s, task_id=%s, instance_id=%s, 已耗时=%sms, metadata=%s",
                stage,
                task_id,
                instance_id,
                elapsed_ms,
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


def _datetime_from_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()
