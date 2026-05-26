from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from agent_runtime.agent.agent_instance import (
    AgentInstance,
    PendingAgentUserMessage,
    PendingUserMessageAttachment,
)
from agent_runtime.agent.agent_execution_engine import (
    AgentExecutionEngine,
    AgentExecutionResult,
    AgentInterruptResult,
    AgentMessageSubmissionResult,
)
from agent_runtime.agent.agent_task import AgentTask
from agent_runtime.agent.north_gateway.agent_runtime_contracts import (
    AgentRuntimeGateway,
    AttachSceneRequest,
    CreateTaskWithRootAgentInstanceRequest,
    DetachSceneRequest,
)
from agent_runtime.agent_definition.north_gateway.agent_definition_contracts import (
    AgentDefinitionNorthGateway,
)
from agent_runtime.conversation_message.conversation_repository import ConversationRepository
from agent_runtime.llm_model_registry.llm_model_registry_service import (
    ResolvedLlmExecutionPolicy,
)
from agent_runtime.llm_model_registry.north_gateway.llm_model_registry_contracts import (
    LlmModelRegistryGateway,
)
from agent_runtime.chat_bubble.chat_bubble_message import (
    CHAT_BUBBLE_STATUS_COMPLETED,
    ChatBubbleInteractionOption,
    ChatBubbleMessage,
    ChatBubbleRole,
    ChatBubbleStatus,
    ChatBubbleTextFormat,
)
from agent_runtime.chat_bubble.chat_bubble_service import ChatBubbleService
from agent_runtime.communication.north_gateway.communication_contracts import CommunicationGateway
from agent_runtime.input_modality import INPUT_MODALITY_IMAGE, INPUT_MODALITY_PDF
from agent_runtime.scene.north_gateway.scene_contracts import SceneGateway
from agent_runtime.scene.scene_session_registry import SceneSessionRegistry

MAX_FILES_PER_LLM_REQUEST = 10
MAX_TOTAL_FILE_BYTES_PER_LLM_REQUEST = 32 * 1024 * 1024
MAX_SINGLE_FILE_BYTES_PER_LLM_REQUEST = 32 * 1024 * 1024
MAX_TOTAL_PDF_PAGES_PER_LLM_REQUEST = 100
MAX_SINGLE_PDF_PAGES_PER_LLM_REQUEST = 100


class CreateRootAgentInstanceBody(BaseModel):
    thread_id: str
    current_working_directory: str
    agent_definition_id: str
    llm_policy_group_id: str | None = None
    llm_provider_id: str | None = None
    llm_model_binding_id: str | None = None
    task_id: str | None = None
    root_instance_id: str | None = None
    conversation_id: str | None = None
    trace_id: str | None = None
    scene_id: str | None = None


class AgentRuntimeTaskResponse(BaseModel):
    task_id: str
    root_agent_instance_id: str
    agent_instance_ids: list[str]
    status: str
    created_at: str
    updated_at: str
    scene_id: str | None = None


class UserMessageAttachmentModel(BaseModel):
    kind: str
    resource_id: str
    name: str | None = None
    mime_type: str | None = None
    size_in_bytes: int | None = None
    width: int | None = None
    height: int | None = None
    page_count: int | None = None


class PendingUserMessageResponse(BaseModel):
    message_id: str
    user_text: str
    client_message_id: str | None = None
    attachments: list[UserMessageAttachmentModel] = Field(default_factory=list)
    received_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentInstanceResponse(BaseModel):
    instance_id: str
    task_id: str
    thread_id: str
    current_working_directory: str
    agent_definition_id: str
    llm_policy_group_id: str | None = None
    llm_policy_snapshot_json: dict[str, Any] = Field(default_factory=dict)
    llm_provider_id: str
    llm_model_binding_id: str
    conversation_id: str | None
    trace_id: str
    status: str
    pending_user_message_count: int = 0
    pending_user_messages: list[PendingUserMessageResponse] = Field(default_factory=list)
    context_usage: dict[str, Any] | None = None
    created_at: str
    updated_at: str


class ChatBubbleInteractionOptionModel(BaseModel):
    id: str
    label: str
    description: str | None = None


class ChatBubbleMessageResponse(BaseModel):
    bubble_id: str
    scene_id: str
    agent_instance_id: str | None = None
    task_id: str | None = None
    conversation_id: str | None = None
    role: str
    status: str
    text: str
    text_format: str
    channel: str | None = None
    client_message_id: str | None = None
    backend_message_id: str | None = None
    pending_message_id: str | None = None
    interaction_options: list[ChatBubbleInteractionOptionModel] = Field(default_factory=list)
    selected_option_id: str | None = None
    sort_order: int
    created_at: str
    updated_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ListChatBubbleMessagesResponse(BaseModel):
    messages: list[ChatBubbleMessageResponse]
    limit: int
    before_sort_order: int | None = None


class CreateChatBubbleMessageBody(BaseModel):
    bubble_id: str | None = None
    agent_instance_id: str | None = None
    task_id: str | None = None
    conversation_id: str | None = None
    role: ChatBubbleRole
    status: ChatBubbleStatus = CHAT_BUBBLE_STATUS_COMPLETED
    text: str
    text_format: ChatBubbleTextFormat = "markdown"
    channel: str | None = "web"
    client_message_id: str | None = None
    backend_message_id: str | None = None
    pending_message_id: str | None = None
    interaction_options: list[ChatBubbleInteractionOptionModel] = Field(default_factory=list)
    selected_option_id: str | None = None
    sort_order: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PatchChatBubbleMessageBody(BaseModel):
    status: ChatBubbleStatus | None = None
    text: str | None = None
    text_format: ChatBubbleTextFormat | None = None
    channel: str | None = None
    backend_message_id: str | None = None
    pending_message_id: str | None = None
    interaction_options: list[ChatBubbleInteractionOptionModel] | None = None
    selected_option_id: str | None = None
    metadata: dict[str, Any] | None = None


class CreateRootAgentInstanceResponse(BaseModel):
    task: AgentRuntimeTaskResponse
    root_agent_instance: AgentInstanceResponse


class SendAgentInstanceMessageBody(BaseModel):
    user_message: str
    attachments: list[UserMessageAttachmentModel] = Field(default_factory=list)
    client_message_id: str | None = None
    render_options: dict[str, Any] = Field(default_factory=dict)
    request_overrides: dict[str, Any] = Field(default_factory=dict)
    communication_stream_id: str | None = None
    message_metadata: dict[str, Any] = Field(default_factory=dict)


class SwitchAgentInstanceLlmPolicyBody(BaseModel):
    llm_policy_group_id: str


class InterruptAgentInstanceBody(BaseModel):
    reason: str = "user_requested"


class AgentInterruptResponse(BaseModel):
    status: str
    task_id: str
    agent_instance_id: str
    conversation_id: str | None = None
    interrupt_requested: bool
    reason: str


class AgentExecutionResponse(BaseModel):
    task_id: str
    agent_instance_id: str
    conversation_id: str
    trace_file_path: str
    communication_stream_id: str | None = None
    assistant_message_text: str | None = None


class AgentMessageSubmissionResponse(BaseModel):
    status: str
    task_id: str
    agent_instance_id: str
    conversation_id: str | None = None
    communication_stream_id: str
    event_stream_url: str
    pending_message_id: str
    client_message_id: str | None = None
    queued_for_later: bool = False
    root_agent_instance: AgentInstanceResponse | None = None
    message_metadata: dict[str, Any] = Field(default_factory=dict)


class RemovePendingUserMessageResponse(BaseModel):
    status: str
    agent_instance: AgentInstanceResponse
    removed_message: PendingUserMessageResponse


class FileInputLimitsResponse(BaseModel):
    max_files_per_request: int
    max_total_file_bytes_per_request: int
    max_single_file_bytes: int
    max_total_pdf_pages_per_request: int
    max_single_pdf_pages: int


class FileInputStatsResponse(BaseModel):
    file_count: int = 0
    total_file_bytes: int = 0
    pdf_file_count: int = 0
    image_file_count: int = 0
    total_pdf_pages: int = 0


class AgentFileInputStateResponse(BaseModel):
    limits: FileInputLimitsResponse
    next_request_file_stats: FileInputStatsResponse


class AttachSceneBody(BaseModel):
    """attach scene 入参。

    ``workspace_path`` 必填：服务端以此读取 ``<workspace>/.spread-ai/project.json`` manifest
    获取项目身份并与全局 scene 数据库交叉校验；同时作为 mode=created 时新建任务的工作目录。

    Fallback LLM 字段仅在 mode=created（scene 下首次创建 root instance）时使用：
    - 优先级 1：body 显式提供 llm_policy_group_id 或 llm_provider_id+llm_model_binding_id；
    - 优先级 2：未提供时回退到 agent_definition.default_llm_policy_group_id；
    - 都没有 → REST 层 400，前端应在 SceneCreator 时让用户选择策略组。
    其余 attach 模式会复用 Scene 已有 root agent instance 的 LLM 策略，忽略所有 LLM 字段。
    每个新 task 都会在第一次用户消息时创建新的 conversation_id，不跨 task 复用旧会话。
    """

    workspace_path: str
    llm_policy_group_id: str | None = None
    llm_provider_id: str | None = None
    llm_model_binding_id: str | None = None


class AttachSceneResponseModel(BaseModel):
    """attach scene 响应。

    mode：resumed | created | recreated。resumed 表示复用 Scene 长期 root agent instance；
    recreated 是旧设计兼容值，新设计默认不再因 task 完成而重建 root agent instance。
    """

    mode: str
    task: AgentRuntimeTaskResponse
    root_agent_instance: AgentInstanceResponse
    communication_stream_id: str
    event_stream_url: str


class DetachSceneBody(BaseModel):
    """detach scene 入参。"""

    workspace_path: str


class DetachSceneResponseModel(BaseModel):
    """detach scene 响应。"""

    cancelled_task_count: int
    cancelled_instance_count: int


class SceneDiagnosticsBody(BaseModel):
    workspace_path: str | None = None
    agent_instance_id: str | None = None
    task_id: str | None = None
    communication_stream_id: str | None = None
    frontend_snapshot: dict[str, Any] = Field(default_factory=dict)


class SceneDiagnosticsResponseModel(BaseModel):
    status: str
    diagnosis: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    matched_rules: list[dict[str, Any]] = Field(default_factory=list)




def create_agent_runtime_router(
    gateway: AgentRuntimeGateway,
    llm_registry_gateway: LlmModelRegistryGateway | None = None,
    execution_engine: AgentExecutionEngine | None = None,
    agent_definition_gateway: AgentDefinitionNorthGateway | None = None,
    scene_gateway: SceneGateway | None = None,
    session_registry: SceneSessionRegistry | None = None,
    chat_bubble_service: ChatBubbleService | None = None,
    conversation_repository: ConversationRepository | None = None,
    file_resource_service: Any | None = None,
    communication_gateway: CommunicationGateway | None = None,
    runtime_health_snapshot: Callable[[], dict[str, Any]] | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/root-instances",
        response_model=CreateRootAgentInstanceResponse,
        status_code=201,
    )
    async def create_root_agent_instance(
        body: CreateRootAgentInstanceBody,
    ) -> CreateRootAgentInstanceResponse:
        try:
            llm_selection = _resolve_llm_runtime_selection(
                body,
                llm_registry_gateway,
                agent_definition_gateway,
            )
        except (KeyError, ValueError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        try:
            task, instance = gateway.create_task_with_root_agent_instance(
                CreateTaskWithRootAgentInstanceRequest(
                    thread_id=body.thread_id,
                    current_working_directory=body.current_working_directory,
                    agent_definition_id=body.agent_definition_id,
                    llm_provider_id=llm_selection.llm_provider_id,
                    llm_model_binding_id=llm_selection.llm_model_binding_id,
                    llm_policy_group_id=llm_selection.llm_policy_group_id,
                    llm_policy_snapshot_json=llm_selection.llm_policy_snapshot_json,
                    task_id=body.task_id,
                    root_instance_id=body.root_instance_id,
                    conversation_id=body.conversation_id,
                    trace_id=body.trace_id,
                    scene_id=body.scene_id,
                ),
            )
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return CreateRootAgentInstanceResponse(
            task=_task_response(task),
            root_agent_instance=await _instance_response(instance, conversation_repository, llm_registry_gateway),
        )

    @router.get("/tasks/{task_id}", response_model=AgentRuntimeTaskResponse)
    def get_task(task_id: str) -> AgentRuntimeTaskResponse:
        try:
            task = gateway.get_task(task_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Agent 任务不存在: {task_id}")
        return _task_response(task)

    @router.get("/instances/{instance_id}", response_model=AgentInstanceResponse)
    async def get_agent_instance(instance_id: str) -> AgentInstanceResponse:
        try:
            instance = gateway.get_agent_instance(instance_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Agent 实例不存在: {instance_id}")
        return await _instance_response(instance, conversation_repository, llm_registry_gateway)

    @router.get(
        "/instances/{instance_id}/file-input-state",
        response_model=AgentFileInputStateResponse,
    )
    async def get_agent_instance_file_input_state(
        instance_id: str,
    ) -> AgentFileInputStateResponse:
        try:
            instance = gateway.get_agent_instance(instance_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Agent 实例不存在: {instance_id}")
        return AgentFileInputStateResponse(
            limits=_file_input_limits_response(),
            next_request_file_stats=await _next_request_file_stats(
                instance,
                conversation_repository,
                file_resource_service,
            ),
        )

    @router.patch("/instances/{instance_id}/llm-policy", response_model=AgentInstanceResponse)
    async def switch_agent_instance_llm_policy(
        instance_id: str,
        body: SwitchAgentInstanceLlmPolicyBody,
    ) -> AgentInstanceResponse:
        if llm_registry_gateway is None:
            raise HTTPException(status_code=501, detail="LLM 注册网关未启用。")
        try:
            instance = gateway.get_agent_instance(instance_id)
            _ensure_agent_allows_policy_group(
                instance.agent_definition_id,
                body.llm_policy_group_id,
                agent_definition_gateway,
            )
            llm_selection = _resolve_llm_policy_group(
                body.llm_policy_group_id,
                llm_registry_gateway,
            )
            updated = gateway.switch_agent_instance_llm_policy(
                instance_id,
                llm_policy_group_id=llm_selection.llm_policy_group_id or body.llm_policy_group_id,
                llm_policy_snapshot_json=llm_selection.llm_policy_snapshot_json,
                llm_provider_id=llm_selection.llm_provider_id,
                llm_model_binding_id=llm_selection.llm_model_binding_id,
            )
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return await _instance_response(updated, conversation_repository, llm_registry_gateway)

    @router.post(
        "/instances/{instance_id}/messages",
        response_model=AgentMessageSubmissionResponse,
        status_code=202,
    )
    async def send_agent_instance_message(
        instance_id: str,
        body: SendAgentInstanceMessageBody,
    ) -> AgentMessageSubmissionResponse:
        if execution_engine is None:
            raise HTTPException(status_code=501, detail="Agent 执行引擎未启用。")
        if not body.user_message.strip() and not body.attachments:
            raise HTTPException(status_code=400, detail="user_message 和 attachments 必须至少提供一个。")
        try:
            result = await execution_engine.submit_agent_instance_message(
                agent_instance_id=instance_id,
                user_text=body.user_message.strip(),
                attachments=tuple(_pending_attachment_from_model(item) for item in body.attachments),
                render_options=body.render_options,
                request_overrides=body.request_overrides,
                communication_stream_id=body.communication_stream_id,
                client_message_id=body.client_message_id,
                message_metadata=body.message_metadata,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Agent 实例不存在: {instance_id}")
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return await _submission_response(result, conversation_repository, llm_registry_gateway)

    @router.post(
        "/instances/{instance_id}/interrupt",
        response_model=AgentInterruptResponse,
        status_code=202,
    )
    async def interrupt_agent_instance(
        instance_id: str,
        body: InterruptAgentInstanceBody | None = None,
    ) -> AgentInterruptResponse:
        if execution_engine is None:
            raise HTTPException(status_code=501, detail="Agent 执行引擎未启用。")
        try:
            result = execution_engine.interrupt_agent_instance(
                instance_id,
                reason=(body.reason if body is not None else "user_requested"),
            )
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Agent 实例不存在: {instance_id}")
        return _interrupt_response(result)

    @router.delete(
        "/instances/{instance_id}/pending-user-messages/{pending_message_id}",
        response_model=RemovePendingUserMessageResponse,
        status_code=200,
    )
    async def remove_agent_instance_pending_user_message(
        instance_id: str,
        pending_message_id: str,
    ) -> RemovePendingUserMessageResponse:
        try:
            updated, removed_message = gateway.remove_agent_instance_user_message(
                instance_id,
                pending_message_id,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Agent 实例不存在: {instance_id}")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if removed_message is None:
            raise HTTPException(
                status_code=409,
                detail="待发送消息不存在或已进入会话发送，无法撤回。",
            )
        return RemovePendingUserMessageResponse(
            status="removed",
            agent_instance=await _instance_response(updated, conversation_repository, llm_registry_gateway),
            removed_message=_pending_user_message_response(removed_message),
        )

    @router.post(
        "/scenes/{scene_id}/attach",
        response_model=AttachSceneResponseModel,
        status_code=200,
    )
    async def attach_scene(
        scene_id: str,
        body: AttachSceneBody,
    ) -> AttachSceneResponseModel:
        """前端进入 scene 的统一入口。

        - 返回 mode=resumed：scene 下已有长期 root agent instance，直接复用或恢复为待命态；
        - 返回 mode=created：scene 下首次创建 root instance（需要 fallback LLM）；
        - mode=recreated 仅为旧设计兼容值，新流程不再因 task 完成而重建 root instance。

        body 中的 LLM 字段仅在 mode=created 时实际生效。REST 层先解析 fallback LLM，
        然后由 gateway 决策真实 mode；其它路径的 fallback LLM 自动忽略。
        """
        if scene_gateway is None:
            raise HTTPException(
                status_code=501,
                detail="Scene 网关未启用，attach 不可用。",
            )
        try:
            scene = scene_gateway.get_scene(body.workspace_path, scene_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Scene 不存在: {scene_id}")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        try:
            fallback = _resolve_attach_fallback_llm(
                body=body,
                bound_agent_definition_id=scene.bound_agent_definition_id,
                llm_registry_gateway=llm_registry_gateway,
                agent_definition_gateway=agent_definition_gateway,
            )
        except (KeyError, ValueError):
            # fallback 解析失败不直接 fail：可能 scene 下已有 instance，attach 走 resumed 不需要 LLM。
            # 把异常吞掉转为空 fallback；若 gateway 走 mode=created 路径会在 gateway 层抛 ValueError。
            fallback = None

        request = AttachSceneRequest(
            workspace_path=body.workspace_path,
            fallback_llm_provider_id=fallback.llm_provider_id if fallback else None,
            fallback_llm_model_binding_id=fallback.llm_model_binding_id if fallback else None,
            fallback_llm_policy_group_id=fallback.llm_policy_group_id if fallback else None,
            fallback_llm_policy_snapshot_json=(
                fallback.llm_policy_snapshot_json if fallback else None
            ),
        )
        try:
            response = gateway.attach_scene(scene_id, request)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except RuntimeError as e:
            raise HTTPException(status_code=501, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))

        # 注册 scene session（心跳追踪）
        if session_registry is not None:
            session_registry.open_session(scene_id, body.workspace_path)

        return AttachSceneResponseModel(
            mode=response.mode,
            task=_task_response(response.task),
            root_agent_instance=await _instance_response(
                response.instance,
                conversation_repository,
                llm_registry_gateway,
            ),
            communication_stream_id=response.communication_stream_id,
            event_stream_url=response.event_stream_url,
        )

    @router.post("/scenes/{scene_id}/heartbeat", status_code=204)
    def scene_heartbeat(scene_id: str) -> None:
        """前端定期发送的 scene 心跳，用于维持 session 活跃状态。

        前端应以 stale_timeout / 3 的间隔（默认约 40s）调用本接口。
        返回 204 表示心跳成功；404 表示 session 不存在，前端应重新 attach。
        """
        if session_registry is None:
            return
        if not session_registry.heartbeat(scene_id):
            raise HTTPException(
                status_code=404,
                detail=f"Scene session 不存在，请重新 attach: {scene_id}",
            )

    @router.post(
        "/scenes/{scene_id}/diagnostics",
        response_model=SceneDiagnosticsResponseModel,
        status_code=200,
    )
    def diagnose_scene(
        scene_id: str,
        body: SceneDiagnosticsBody,
    ) -> SceneDiagnosticsResponseModel:
        frontend_snapshot = dict(body.frontend_snapshot)
        instance, task, instance_error = _resolve_diagnostics_instance(
            gateway,
            scene_id=scene_id,
            instance_id=body.agent_instance_id or _optional_text(frontend_snapshot.get("agent_instance_id")),
            task_id=body.task_id or _optional_text(frontend_snapshot.get("task_id")),
        )
        stream_id = (
            body.communication_stream_id
            or _optional_text(frontend_snapshot.get("communication_stream_id"))
            or _optional_text(frontend_snapshot.get("stream_id"))
            or f"stream-scene-{scene_id}"
        )
        runtime_snapshot = (
            execution_engine.get_agent_execution_diagnostics(instance.instance_id)
            if execution_engine is not None and instance is not None
            else None
        )
        communication_snapshot = _communication_diagnostics_snapshot(communication_gateway, stream_id)
        session_snapshot = (
            session_registry.get_session_diagnostics(scene_id)
            if session_registry is not None
            else None
        )
        evidence = {
            "frontend": frontend_snapshot,
            "backend_control_plane": runtime_health_snapshot() if runtime_health_snapshot is not None else None,
            "backend_instance": _agent_instance_diagnostics(instance) if instance is not None else None,
            "backend_task": _agent_task_diagnostics(task) if task is not None else None,
            "backend_instance_error": instance_error,
            "backend_runtime": runtime_snapshot,
            "communication": communication_snapshot,
            "scene_session": session_snapshot,
        }
        diagnosis, matched_rules = _analyze_scene_diagnostics(evidence)
        return SceneDiagnosticsResponseModel(
            status="analyzed",
            diagnosis=diagnosis,
            evidence=evidence,
            matched_rules=matched_rules,
        )

    @router.post("/scenes/{scene_id}/detach", status_code=200)
    def detach_scene(
        scene_id: str,
        body: DetachSceneBody,
    ) -> DetachSceneResponseModel:
        """前端主动断开 scene（关闭 tab / beforeunload 时调用）。

        执行逻辑：移除 session 注册 + 取消 scene 下非终态 task/instances。
        """
        # 移除 session 注册
        if session_registry is not None:
            session_registry.close_session(scene_id)

        request = DetachSceneRequest(workspace_path=body.workspace_path)
        response = gateway.detach_scene(scene_id, request)
        return DetachSceneResponseModel(
            cancelled_task_count=response.cancelled_task_count,
            cancelled_instance_count=response.cancelled_instance_count,
        )

    @router.get("/scenes/{scene_id}/chat-bubbles", response_model=ListChatBubbleMessagesResponse)
    def list_scene_chat_bubbles(
        scene_id: str,
        limit: int = Query(default=100, ge=1, le=500),
        before_sort_order: int | None = Query(default=None, ge=0),
        agent_instance_id: str | None = None,
    ) -> ListChatBubbleMessagesResponse:
        """读取 Scene 的前端历史消息气泡。

        Chat Bubble 是前端 UI 展示状态，不从 communication event log 回放生成。前端打开 Scene
        时应调用本接口恢复消息气泡列表；事件日志仍只用于 SSE、审计和调试。
        """
        if chat_bubble_service is None:
            raise HTTPException(status_code=501, detail="Chat Bubble 服务未启用。")
        try:
            messages = chat_bubble_service.list_scene_messages(
                scene_id,
                limit=limit,
                before_sort_order=before_sort_order,
                agent_instance_id=agent_instance_id,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return ListChatBubbleMessagesResponse(
            messages=[_chat_bubble_response(message) for message in messages],
            limit=limit,
            before_sort_order=before_sort_order,
        )

    @router.post(
        "/scenes/{scene_id}/chat-bubbles",
        response_model=ChatBubbleMessageResponse,
        status_code=201,
    )
    def create_scene_chat_bubble(
        scene_id: str,
        body: CreateChatBubbleMessageBody,
    ) -> ChatBubbleMessageResponse:
        """创建一条前端消息气泡。

        前端在用户消息真正进入 chat 列表、assistant 消息展示或交互问题展示时调用；
        queued_for_later 的用户消息只展示在浮层，暂不写入 Chat Bubble 表。
        """
        if chat_bubble_service is None:
            raise HTTPException(status_code=501, detail="Chat Bubble 服务未启用。")
        try:
            message = chat_bubble_service.create_message(
                scene_id=scene_id,
                bubble_id=body.bubble_id,
                agent_instance_id=body.agent_instance_id,
                task_id=body.task_id,
                conversation_id=body.conversation_id,
                role=body.role,
                status=body.status,
                text=body.text,
                text_format=body.text_format,
                channel=body.channel,
                client_message_id=body.client_message_id,
                backend_message_id=body.backend_message_id,
                pending_message_id=body.pending_message_id,
                interaction_options=_interaction_options_tuple(body.interaction_options),
                selected_option_id=body.selected_option_id,
                sort_order=body.sort_order,
                metadata=body.metadata,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return _chat_bubble_response(message)

    @router.patch(
        "/scenes/{scene_id}/chat-bubbles/{bubble_id}",
        response_model=ChatBubbleMessageResponse,
    )
    def patch_scene_chat_bubble(
        scene_id: str,
        bubble_id: str,
        body: PatchChatBubbleMessageBody,
    ) -> ChatBubbleMessageResponse:
        """更新一条前端消息气泡展示状态。"""
        if chat_bubble_service is None:
            raise HTTPException(status_code=501, detail="Chat Bubble 服务未启用。")
        try:
            message = chat_bubble_service.patch_message(
                scene_id=scene_id,
                bubble_id=bubble_id,
                status=body.status,
                text=body.text,
                text_format=body.text_format,
                channel=body.channel,
                backend_message_id=body.backend_message_id,
                pending_message_id=body.pending_message_id,
                interaction_options=(
                    _interaction_options_tuple(body.interaction_options)
                    if body.interaction_options is not None
                    else None
                ),
                selected_option_id=body.selected_option_id,
                metadata=body.metadata,
            )
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return _chat_bubble_response(message)

    @router.delete("/scenes/{scene_id}/chat-bubbles/{bubble_id}", status_code=204)
    def delete_scene_chat_bubble(scene_id: str, bubble_id: str) -> None:
        """删除一条前端消息气泡。"""
        if chat_bubble_service is None:
            raise HTTPException(status_code=501, detail="Chat Bubble 服务未启用。")
        try:
            chat_bubble_service.delete_message(scene_id=scene_id, bubble_id=bubble_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    return router


def _resolve_diagnostics_instance(
    gateway: AgentRuntimeGateway,
    *,
    scene_id: str,
    instance_id: str | None,
    task_id: str | None,
) -> tuple[AgentInstance | None, AgentTask | None, str | None]:
    try:
        if instance_id:
            instance = gateway.get_agent_instance(instance_id)
            return instance, gateway.get_task(instance.task_id), None
        if task_id:
            task = gateway.get_task(task_id)
            if not task.agent_instance_ids:
                return None, task, "任务没有关联 Agent 实例。"
            instance = gateway.get_agent_instance(task.root_agent_instance_id)
            return instance, task, None
        tasks = gateway.list_scene_tasks(scene_id)
        if not tasks:
            return None, None, "Scene 下没有 Agent 任务。"
        task = tasks[0]
        instance = gateway.get_agent_instance(task.root_agent_instance_id)
        return instance, task, None
    except KeyError as exc:
        return None, None, str(exc)


def _communication_diagnostics_snapshot(
    communication_gateway: CommunicationGateway | None,
    stream_id: str | None,
) -> dict[str, Any] | None:
    if communication_gateway is None or not stream_id:
        return None
    try:
        return dict(communication_gateway.get_stream_diagnostics(stream_id))
    except KeyError:
        return {"stream_id": stream_id, "exists": False}


def _agent_instance_diagnostics(instance: AgentInstance) -> dict[str, Any]:
    return {
        "instance_id": instance.instance_id,
        "task_id": instance.task_id,
        "conversation_id": instance.conversation_id,
        "status": instance.status,
        "pending_user_message_count": len(instance.pending_user_messages),
        "updated_at": _datetime_text(instance.updated_at),
        "created_at": _datetime_text(instance.created_at),
    }


def _agent_task_diagnostics(task: AgentTask) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "root_agent_instance_id": task.root_agent_instance_id,
        "agent_instance_ids": list(task.agent_instance_ids),
        "status": task.status,
        "scene_id": task.scene_id,
        "updated_at": _datetime_text(task.updated_at),
        "created_at": _datetime_text(task.created_at),
    }


def _analyze_scene_diagnostics(
    evidence: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    matched: list[dict[str, Any]] = []
    frontend = _dict_value(evidence.get("frontend"))
    control_plane = _dict_value(evidence.get("backend_control_plane"))
    backend_instance = _dict_value(evidence.get("backend_instance"))
    runtime = _dict_value(evidence.get("backend_runtime"))
    communication = _dict_value(evidence.get("communication"))
    session = _dict_value(evidence.get("scene_session"))

    frontend_run_status = _optional_text(frontend.get("run_status"))
    backend_status = _optional_text(backend_instance.get("status"))
    active_handle = runtime.get("active_handle_exists") is True
    latest_seq = _int_or_none(communication.get("latest_seq")) if communication else None
    frontend_last_seq = (
        _int_or_none(frontend.get("frontend_last_seq"))
        or _int_or_none(frontend.get("last_seq"))
        or _int_or_none(frontend.get("cursor"))
    )
    event_loop = _dict_value(control_plane.get("event_loop"))
    event_loop_status = _optional_text(event_loop.get("status"))
    blocked_tick_count = _int_or_none(event_loop.get("blocked_tick_count")) or 0
    max_lag_ms = _int_or_none(event_loop.get("max_lag_ms")) or 0

    if event_loop_status in {"blocked_recently", "slow_recently"} or blocked_tick_count > 0 or max_lag_ms >= 2000:
        matched.append(_diagnostic_rule("backend_control_plane_lag", "后端控制面 event loop 出现明显调度延迟。"))
        return _diagnosis(
            "backend_control_plane_lag",
            "error" if event_loop_status == "blocked_recently" or blocked_tick_count > 0 else "warning",
            "后端控制面调度延迟",
            f"后端 event loop maxLag={max_lag_ms}ms，blockedTicks={blocked_tick_count}，status={event_loop_status or 'unknown'}。",
        ), matched

    if evidence.get("backend_instance_error"):
        matched.append(_diagnostic_rule("backend_instance_missing", "后端没有找到当前 Agent 实例或任务。"))
        return _diagnosis(
            "backend_instance_missing",
            "error",
            "后端未找到当前 Agent 实例",
            "诊断请求携带的实例或任务无法在后端运行态中定位。",
        ), matched

    if frontend_run_status == "running" and backend_status and backend_status != "running" and not active_handle:
        matched.append(_diagnostic_rule("frontend_stale_running", "前端显示 running，但后端实例不是 running 且没有 active handle。"))
        return _diagnosis(
            "frontend_stale_running",
            "warning",
            "前端执行中状态陈旧",
            f"前端 runStatus=running，后端实例 status={backend_status}，执行引擎没有活动任务。",
        ), matched

    if backend_status == "running" and not active_handle:
        matched.append(_diagnostic_rule("backend_runtime_state_leak", "后端实例 status=running，但执行引擎内存中没有 active handle。"))
        return _diagnosis(
            "backend_runtime_state_leak",
            "error",
            "后端运行态不一致",
            "AgentInstance 持久态为 running，但当前进程执行引擎没有对应执行任务。",
        ), matched

    if latest_seq is not None and frontend_last_seq is not None and latest_seq > frontend_last_seq:
        matched.append(_diagnostic_rule("frontend_sse_cursor_lag", "后端通信流 seq 已前进，但前端 cursor 落后。"))
        return _diagnosis(
            "frontend_sse_cursor_lag",
            "warning",
            "前端事件消费落后",
            f"后端 latest_seq={latest_seq}，前端 lastSeq={frontend_last_seq}，说明后端已有事件未被前端应用。",
        ), matched

    if active_handle:
        progress = _dict_value(runtime.get("progress"))
        phase = _optional_text(progress.get("phase")) or "unknown"
        elapsed_ms = _int_or_none(progress.get("phase_elapsed_ms")) or 0
        if phase == "llm_prompt":
            matched.append(_diagnostic_rule("backend_llm_waiting", "执行引擎 active handle 存在，当前阶段为 LLM 请求。"))
            return _diagnosis(
                "backend_llm_waiting",
                "warning" if elapsed_ms >= 60000 else "info",
                "后端正在等待 LLM 响应",
                f"Agent 执行循环处于 llm_prompt 阶段，已持续 {elapsed_ms // 1000} 秒。",
            ), matched
        if phase == "tool_batch":
            matched.append(_diagnostic_rule("backend_tool_waiting", "执行引擎 active handle 存在，当前阶段为工具批执行。"))
            return _diagnosis(
                "backend_tool_waiting",
                "warning" if elapsed_ms >= 60000 else "info",
                "后端正在执行工具",
                f"Agent 执行循环处于 tool_batch 阶段，已持续 {elapsed_ms // 1000} 秒。",
            ), matched
        if phase == "context_compression":
            matched.append(_diagnostic_rule("backend_context_compression", "执行引擎 active handle 存在，当前阶段为上下文压缩。"))
            return _diagnosis(
                "backend_context_compression",
                "info",
                "后端正在执行上下文压缩",
                f"Agent 执行循环处于 context_compression 阶段，已持续 {elapsed_ms // 1000} 秒。",
            ), matched
        matched.append(_diagnostic_rule("backend_active", "执行引擎 active handle 存在。"))
        return _diagnosis(
            "backend_active",
            "info",
            "后端 Agent 执行循环仍在运行",
            f"当前执行阶段为 {phase}，已持续 {elapsed_ms // 1000} 秒。",
        ), matched

    if session and session.get("is_open") is False:
        matched.append(_diagnostic_rule("scene_session_missing", "后端 scene session 不存在。"))
        return _diagnosis(
            "scene_session_missing",
            "warning",
            "后端 Scene session 不存在",
            "后端没有当前 scene 的 heartbeat session 记录。",
        ), matched

    matched.append(_diagnostic_rule("state_consistent", "未发现前后端状态机矛盾或活动执行卡点。"))
    return _diagnosis(
        "state_consistent",
        "ok",
        "未发现明确故障",
        "前端、后端实例、执行引擎和通信流快照未命中异常规则。",
    ), matched


def _diagnosis(category: str, severity: str, title: str, description: str) -> dict[str, Any]:
    return {
        "category": category,
        "severity": severity,
        "title": title,
        "description": description,
    }


def _diagnostic_rule(rule_id: str, reason: str) -> dict[str, Any]:
    return {"rule_id": rule_id, "reason": reason}


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True, slots=True)
class _LlmRuntimeSelection:
    llm_provider_id: str
    llm_model_binding_id: str
    llm_policy_group_id: str | None = None
    llm_policy_snapshot_json: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.llm_policy_snapshot_json is None:
            object.__setattr__(self, "llm_policy_snapshot_json", {})


def _resolve_llm_runtime_selection(
    body: CreateRootAgentInstanceBody,
    llm_registry_gateway: LlmModelRegistryGateway | None,
    agent_definition_gateway: AgentDefinitionNorthGateway | None,
) -> _LlmRuntimeSelection:
    policy_group_id = body.llm_policy_group_id
    direct_binding_requested = bool(body.llm_provider_id or body.llm_model_binding_id)
    if (
        policy_group_id is None
        and not direct_binding_requested
        and agent_definition_gateway is not None
    ):
        definition = agent_definition_gateway.get_agent_definition(body.agent_definition_id)
        policy_group_id = definition.default_llm_policy_group_id
    if policy_group_id:
        _ensure_agent_allows_policy_group(
            body.agent_definition_id,
            policy_group_id,
            agent_definition_gateway,
        )
        if llm_registry_gateway is None:
            raise ValueError("LLM 注册网关未启用，不能解析 LLM 执行策略组。")
        return _resolve_llm_policy_group(policy_group_id, llm_registry_gateway)
    llm_provider_id, llm_model_binding_id = _resolve_llm_binding(body, llm_registry_gateway)
    return _LlmRuntimeSelection(
        llm_provider_id=llm_provider_id,
        llm_model_binding_id=llm_model_binding_id,
    )


def _resolve_attach_fallback_llm(
    *,
    body: AttachSceneBody,
    bound_agent_definition_id: str,
    llm_registry_gateway: LlmModelRegistryGateway | None,
    agent_definition_gateway: AgentDefinitionNorthGateway | None,
) -> _LlmRuntimeSelection | None:
    """为 attach 接口解析 fallback LLM。

    优先级：body.llm_policy_group_id > body.llm_provider_id+llm_model_binding_id > agent_definition.default_llm_policy_group_id。
    任一路径都没有可解析信息时返回 None；解析失败抛 ValueError 由调用方处理。
    """
    policy_group_id = body.llm_policy_group_id
    direct_binding_requested = bool(body.llm_provider_id or body.llm_model_binding_id)
    if (
        policy_group_id is None
        and not direct_binding_requested
        and agent_definition_gateway is not None
    ):
        definition = agent_definition_gateway.get_agent_definition(bound_agent_definition_id)
        policy_group_id = definition.default_llm_policy_group_id
    if policy_group_id:
        _ensure_agent_allows_policy_group(
            bound_agent_definition_id,
            policy_group_id,
            agent_definition_gateway,
        )
        if llm_registry_gateway is None:
            raise ValueError("LLM 注册网关未启用，不能解析 LLM 执行策略组。")
        return _resolve_llm_policy_group(policy_group_id, llm_registry_gateway)
    if body.llm_provider_id and body.llm_model_binding_id:
        return _LlmRuntimeSelection(
            llm_provider_id=body.llm_provider_id,
            llm_model_binding_id=body.llm_model_binding_id,
        )
    return None


def _resolve_llm_binding(
    body: CreateRootAgentInstanceBody,
    llm_registry_gateway: LlmModelRegistryGateway | None,
) -> tuple[str, str]:
    if body.llm_provider_id and body.llm_model_binding_id:
        return body.llm_provider_id, body.llm_model_binding_id
    if llm_registry_gateway is None:
        raise ValueError("必须指定 llm_provider_id 和 llm_model_binding_id。")
    if body.llm_provider_id:
        provider = llm_registry_gateway.get_provider(body.llm_provider_id)
        if not provider.models:
            raise ValueError(f"LLM 服务商没有模型接入项: {body.llm_provider_id}")
        return provider.provider_id, provider.models[0].id
    if body.llm_model_binding_id:
        for provider in llm_registry_gateway.list_providers():
            if any(model.id == body.llm_model_binding_id for model in provider.models):
                return provider.provider_id, body.llm_model_binding_id
        raise ValueError(f"LLM 模型接入项不存在: {body.llm_model_binding_id}")
    for provider in llm_registry_gateway.list_providers():
        if provider.models:
            return provider.provider_id, provider.models[0].id
    raise ValueError("未找到可用的 LLM 服务商模型接入项。")


def _ensure_agent_allows_policy_group(
    agent_definition_id: str,
    policy_group_id: str,
    agent_definition_gateway: AgentDefinitionNorthGateway | None,
) -> None:
    if agent_definition_gateway is None:
        return
    definition = agent_definition_gateway.get_agent_definition(agent_definition_id)
    if (
        definition.allowed_llm_policy_group_ids
        and policy_group_id not in definition.allowed_llm_policy_group_ids
    ):
        raise ValueError(
            f"Agent 定义不允许使用 LLM 执行策略组: {agent_definition_id}/{policy_group_id}"
        )


def _resolve_llm_policy_group(
    policy_group_id: str,
    llm_registry_gateway: LlmModelRegistryGateway,
) -> _LlmRuntimeSelection:
    resolved = llm_registry_gateway.resolve_execution_policy(policy_group_id)
    if not resolved.candidates:
        raise ValueError(f"LLM 执行策略组没有可用候选项: {policy_group_id}")
    first = resolved.candidates[0]
    return _LlmRuntimeSelection(
        llm_provider_id=first.provider_id,
        llm_model_binding_id=first.model_binding_id,
        llm_policy_group_id=resolved.policy_group_id,
        llm_policy_snapshot_json=_policy_snapshot_without_secrets(resolved),
    )


def _policy_snapshot_without_secrets(
    resolved: ResolvedLlmExecutionPolicy,
) -> dict[str, Any]:
    return {
        "policy_group_id": resolved.policy_group_id,
        "name": resolved.name,
        "description": resolved.description,
        "unsupported_input_modality_strategy": resolved.unsupported_input_modality_strategy,
        "candidates": [
            {
                "candidate_id": candidate.candidate_id,
                "provider_id": candidate.provider_id,
                "model_binding_id": candidate.model_binding_id,
                "model_id": candidate.resolved_model.model_id,
                "model_alias": candidate.resolved_model.model_alias,
                "api_format": candidate.resolved_model.api_format,
                "sdk_platform": candidate.resolved_model.sdk_platform,
                "context_window": candidate.resolved_model.context_window,
            }
            for candidate in resolved.candidates
        ],
    }


def _task_response(task: AgentTask) -> AgentRuntimeTaskResponse:
    return AgentRuntimeTaskResponse(
        task_id=task.task_id,
        root_agent_instance_id=task.root_agent_instance_id,
        agent_instance_ids=list(task.agent_instance_ids),
        status=task.status,
        created_at=_datetime_text(task.created_at),
        updated_at=_datetime_text(task.updated_at),
        scene_id=task.scene_id,
    )


def _file_input_limits_response() -> FileInputLimitsResponse:
    return FileInputLimitsResponse(
        max_files_per_request=MAX_FILES_PER_LLM_REQUEST,
        max_total_file_bytes_per_request=MAX_TOTAL_FILE_BYTES_PER_LLM_REQUEST,
        max_single_file_bytes=MAX_SINGLE_FILE_BYTES_PER_LLM_REQUEST,
        max_total_pdf_pages_per_request=MAX_TOTAL_PDF_PAGES_PER_LLM_REQUEST,
        max_single_pdf_pages=MAX_SINGLE_PDF_PAGES_PER_LLM_REQUEST,
    )


async def _next_request_file_stats(
    instance: AgentInstance,
    conversation_repository: ConversationRepository | None,
    file_resource_service: Any | None,
) -> FileInputStatsResponse:
    stats = _empty_file_input_stats()
    if conversation_repository is not None and instance.conversation_id:
        conversation = await conversation_repository.get(instance.conversation_id)
        _add_resource_reference_stats(
            stats,
            list(_iter_file_resource_references(conversation.messages)),
            file_resource_service,
        )
    for message in instance.pending_user_messages:
        for attachment in message.attachments:
            _add_file_input_stats(
                stats,
                attachment.kind,
                attachment.size_in_bytes or 0,
                attachment.page_count or 0,
            )
    return FileInputStatsResponse(**stats)


def _empty_file_input_stats() -> dict[str, int]:
    return {
        "file_count": 0,
        "total_file_bytes": 0,
        "pdf_file_count": 0,
        "image_file_count": 0,
        "total_pdf_pages": 0,
    }


def _iter_file_resource_references(messages: list[Any]):
    for message in messages:
        yield from _iter_file_resource_references_from_parts(getattr(message, "content", []))


def _iter_file_resource_references_from_parts(parts: list[Any]):
    for part in parts:
        kind = getattr(part, "kind", None)
        if kind in {INPUT_MODALITY_IMAGE, INPUT_MODALITY_PDF}:
            resource_id = getattr(part, "resource_id", None)
            if resource_id:
                yield kind, str(resource_id)
        elif kind == "tool_result":
            output = getattr(part, "output", None)
            if isinstance(output, list):
                yield from _iter_file_resource_references_from_parts(output)


def _add_resource_reference_stats(
    stats: dict[str, int],
    references: list[tuple[str, str]],
    file_resource_service: Any | None,
) -> None:
    if not references:
        return
    if file_resource_service is None:
        raise RuntimeError("文件资源服务未启用，无法统计历史文件输入。")
    resources = file_resource_service.get_resources([resource_id for _, resource_id in references])
    for index, (kind, _resource_id) in enumerate(references):
        resource = resources[index] if index < len(resources) else None
        if resource is None:
            raise RuntimeError("历史文件资源不存在，无法统计文件输入限制。")
        metadata = getattr(resource, "resource_metadata", None)
        category = getattr(metadata, "resource_category", None)
        actual_kind = (
            INPUT_MODALITY_PDF
            if category == INPUT_MODALITY_PDF
            else INPUT_MODALITY_IMAGE
            if category == INPUT_MODALITY_IMAGE
            else kind
        )
        _add_file_input_stats(
            stats,
            actual_kind,
            _resource_size(resource),
            getattr(metadata, "pdf_page_count", 0) if actual_kind == INPUT_MODALITY_PDF else 0,
        )


def _resource_size(resource: Any) -> int:
    try:
        return max(0, int(getattr(resource, "resource_size_in_bytes", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _add_file_input_stats(
    stats: dict[str, int],
    kind: str,
    size_in_bytes: int,
    page_count: int,
) -> None:
    stats["file_count"] += 1
    stats["total_file_bytes"] += max(0, size_in_bytes)
    if kind == INPUT_MODALITY_PDF:
        stats["pdf_file_count"] += 1
        stats["total_pdf_pages"] += max(0, page_count)
        return
    if kind == INPUT_MODALITY_IMAGE:
        stats["image_file_count"] += 1


async def _instance_response(
    instance: AgentInstance,
    conversation_repository: ConversationRepository | None = None,
    llm_registry_gateway: LlmModelRegistryGateway | None = None,
) -> AgentInstanceResponse:
    return AgentInstanceResponse(
        instance_id=instance.instance_id,
        task_id=instance.task_id,
        thread_id=instance.thread_id,
        current_working_directory=instance.current_working_directory,
        agent_definition_id=instance.agent_definition_id,
        llm_policy_group_id=instance.llm_policy_group_id,
        llm_policy_snapshot_json=instance.llm_policy_snapshot_json,
        llm_provider_id=instance.llm_provider_id,
        llm_model_binding_id=instance.llm_model_binding_id,
        conversation_id=instance.conversation_id,
        trace_id=instance.trace_id,
        status=instance.status,
        pending_user_message_count=len(instance.pending_user_messages),
        pending_user_messages=[
            _pending_user_message_response(message)
            for message in instance.pending_user_messages
        ],
        context_usage=await _instance_context_usage(
            instance,
            conversation_repository,
            llm_registry_gateway,
        ),
        created_at=_datetime_text(instance.created_at),
        updated_at=_datetime_text(instance.updated_at),
    )


async def _instance_context_usage(
    instance: AgentInstance,
    conversation_repository: ConversationRepository | None,
    llm_registry_gateway: LlmModelRegistryGateway | None,
) -> dict[str, Any] | None:
    fallback = _fallback_context_usage(instance, llm_registry_gateway)
    usage = None
    if conversation_repository is not None and instance.conversation_id:
        try:
            conversation = await conversation_repository.get(instance.conversation_id)
            usage = _normalize_context_usage(conversation.state.last_context_usage)
        except Exception:
            usage = None
    if usage is None:
        return fallback
    if usage.get("context_window_max") is None and fallback is not None:
        usage = {**fallback, **usage, "context_window_max": fallback.get("context_window_max")}
    return usage


def _normalize_context_usage(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    token_usage = _normalize_token_usage(value.get("token_usage"))
    used_tokens = _int_or_none(value.get("context_window_used_tokens"))
    if used_tokens is None:
        used_tokens = int(token_usage.get("total_tokens") or 0) if token_usage else 0
    result = {
        "token_usage": token_usage,
        "context_window_used_tokens": used_tokens,
        "context_window_max": _int_or_none(value.get("context_window_max")),
        "provider_id": _optional_text(value.get("provider_id")),
        "binding_id": _optional_text(value.get("binding_id")),
        "model_id": _optional_text(value.get("model_id")),
        "model_alias": _optional_text(value.get("model_alias")),
    }
    if result["context_window_max"] is None and token_usage is None:
        return None
    return result


def _normalize_token_usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    return {
        "input_tokens": _non_negative_int(value.get("input_tokens")),
        "output_tokens": _non_negative_int(value.get("output_tokens")),
        "total_tokens": _non_negative_int(value.get("total_tokens")),
        "cached_tokens": _non_negative_int(value.get("cached_tokens")),
        **(
            {"reasoning_tokens": _non_negative_int(value.get("reasoning_tokens"))}
            if value.get("reasoning_tokens") is not None
            else {}
        ),
        **(
            {"cache_creation_tokens": _non_negative_int(value.get("cache_creation_tokens"))}
            if value.get("cache_creation_tokens") is not None
            else {}
        ),
    }


def _fallback_context_usage(
    instance: AgentInstance,
    llm_registry_gateway: LlmModelRegistryGateway | None,
) -> dict[str, Any] | None:
    candidate = _first_policy_candidate(instance)
    if llm_registry_gateway is not None and candidate is not None:
        provider_id = candidate.get("provider_id")
        model_binding_id = candidate.get("model_binding_id")
        if provider_id and model_binding_id:
            try:
                resolved = llm_registry_gateway.resolve_provider_model(
                    str(provider_id),
                    str(model_binding_id),
                )
                return _context_usage_from_model(
                    provider_id=resolved.provider_id,
                    binding_id=resolved.binding_id,
                    model_id=resolved.model_id,
                    model_alias=resolved.model_alias,
                    context_window_max=resolved.context_window,
                )
            except (KeyError, ValueError):
                pass
    if candidate is not None:
        context_window = _int_or_none(candidate.get("context_window"))
        if context_window is not None:
            return _context_usage_from_model(
                provider_id=_optional_text(candidate.get("provider_id")),
                binding_id=_optional_text(candidate.get("model_binding_id")),
                model_id=_optional_text(candidate.get("model_id")),
                model_alias=_optional_text(candidate.get("model_alias")),
                context_window_max=context_window,
            )
    if llm_registry_gateway is not None:
        try:
            resolved = llm_registry_gateway.resolve_provider_model(
                instance.llm_provider_id,
                instance.llm_model_binding_id,
            )
            return _context_usage_from_model(
                provider_id=resolved.provider_id,
                binding_id=resolved.binding_id,
                model_id=resolved.model_id,
                model_alias=resolved.model_alias,
                context_window_max=resolved.context_window,
            )
        except (KeyError, ValueError):
            return None
    return None


def _first_policy_candidate(instance: AgentInstance) -> dict[str, Any] | None:
    candidates = instance.llm_policy_snapshot_json.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None
    first = candidates[0]
    return first if isinstance(first, dict) else None


def _context_usage_from_model(
    *,
    provider_id: str | None,
    binding_id: str | None,
    model_id: str | None,
    model_alias: str | None,
    context_window_max: int,
) -> dict[str, Any]:
    return {
        "token_usage": None,
        "context_window_used_tokens": 0,
        "context_window_max": context_window_max,
        "provider_id": provider_id,
        "binding_id": binding_id,
        "model_id": model_id,
        "model_alias": model_alias,
    }


def _int_or_none(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _non_negative_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _pending_user_message_response(message: PendingAgentUserMessage) -> PendingUserMessageResponse:
    return PendingUserMessageResponse(
        message_id=message.message_id,
        user_text=message.user_text,
        client_message_id=message.client_message_id,
        attachments=[_attachment_model(attachment) for attachment in message.attachments],
        received_at=_datetime_text(message.received_at),
        metadata=message.metadata,
    )


def _pending_attachment_from_model(model: UserMessageAttachmentModel) -> PendingUserMessageAttachment:
    return PendingUserMessageAttachment(
        kind=model.kind,
        resource_id=model.resource_id,
        name=model.name,
        mime_type=model.mime_type,
        size_in_bytes=model.size_in_bytes,
        width=model.width,
        height=model.height,
        page_count=model.page_count,
    )


def _attachment_model(attachment: PendingUserMessageAttachment) -> UserMessageAttachmentModel:
    return UserMessageAttachmentModel(
        kind=attachment.kind,
        resource_id=attachment.resource_id,
        name=attachment.name,
        mime_type=attachment.mime_type,
        size_in_bytes=attachment.size_in_bytes,
        width=attachment.width,
        height=attachment.height,
        page_count=attachment.page_count,
    )


def _interaction_options_tuple(
    options: list[ChatBubbleInteractionOptionModel],
) -> tuple[ChatBubbleInteractionOption, ...]:
    return tuple(
        ChatBubbleInteractionOption(
            option_id=option.id,
            label=option.label,
            description=option.description,
        )
        for option in options
    )


def _chat_bubble_response(message: ChatBubbleMessage) -> ChatBubbleMessageResponse:
    return ChatBubbleMessageResponse(
        bubble_id=message.bubble_id,
        scene_id=message.scene_id,
        agent_instance_id=message.agent_instance_id,
        task_id=message.task_id,
        conversation_id=message.conversation_id,
        role=message.role,
        status=message.status,
        text=message.text,
        text_format=message.text_format,
        channel=message.channel,
        client_message_id=message.client_message_id,
        backend_message_id=message.backend_message_id,
        pending_message_id=message.pending_message_id,
        interaction_options=[
            ChatBubbleInteractionOptionModel(**option.to_json())
            for option in message.interaction_options
        ],
        selected_option_id=message.selected_option_id,
        sort_order=message.sort_order,
        created_at=_datetime_text(message.created_at),
        updated_at=_datetime_text(message.updated_at),
        metadata=message.metadata,
    )


def _execution_response(result: AgentExecutionResult) -> AgentExecutionResponse:
    return AgentExecutionResponse(
        task_id=result.task_id,
        agent_instance_id=result.instance_id,
        conversation_id=result.conversation_id,
        trace_file_path=result.trace_file_path,
        communication_stream_id=result.communication_stream_id,
        assistant_message_text=_assistant_message_text(result.assistant_message),
    )


def _interrupt_response(result: AgentInterruptResult) -> AgentInterruptResponse:
    return AgentInterruptResponse(
        status=result.status,
        task_id=result.task_id,
        agent_instance_id=result.instance_id,
        conversation_id=result.conversation_id,
        interrupt_requested=result.interrupt_requested,
        reason=result.reason,
    )


async def _submission_response(
    result: AgentMessageSubmissionResult,
    conversation_repository: ConversationRepository | None = None,
    llm_registry_gateway: LlmModelRegistryGateway | None = None,
) -> AgentMessageSubmissionResponse:
    return AgentMessageSubmissionResponse(
        status=result.status,
        task_id=result.task_id,
        agent_instance_id=result.instance_id,
        conversation_id=result.conversation_id,
        communication_stream_id=result.communication_stream_id,
        event_stream_url=f"/api/communication/streams/{result.communication_stream_id}/events",
        pending_message_id=result.pending_message_id,
        client_message_id=result.client_message_id,
        queued_for_later=result.queued_for_later,
        message_metadata=result.message_metadata,
        root_agent_instance=await _instance_response(
            result.instance,
            conversation_repository,
            llm_registry_gateway,
        ),
    )


def _assistant_message_text(message: object | None) -> str | None:
    if message is None:
        return None
    parts = getattr(message, "content", [])
    texts = [
        part.text
        for part in parts
        if getattr(part, "kind", None) == "text" and getattr(part, "text", None)
    ]
    return "\n\n".join(texts) if texts else None


def _datetime_text(value: datetime) -> str:
    return value.isoformat()
