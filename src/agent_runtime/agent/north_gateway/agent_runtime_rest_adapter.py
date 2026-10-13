from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
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
from agent_runtime.agent.agent_task import AgentParentChildRelation, AgentTask
from agent_runtime.agent.task_completion import SceneTaskCompletionOutcome, SceneTaskCompletionResult
from agent_runtime.agent.file_input_policy import (
    collect_next_request_file_input_stats,
    file_input_limits_payload,
    file_input_stats_payload,
)
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
from agent_runtime.llm_model_registry.north_gateway.llm_model_registry_contracts import (
    LlmModelRegistryGateway,
)
from agent_runtime.chat_bubble.chat_bubble_message import (
    CHAT_BUBBLE_STATUS_COMPLETED,
    ChatBubbleInteractionOption,
    ChatBubbleInteractionState,
    ChatBubbleMessage,
    ChatBubbleRole,
    ChatBubbleStatus,
    ChatBubbleTextFormat,
)
from agent_runtime.chat_bubble.chat_bubble_service import ChatBubbleService
from agent_runtime.scene.north_gateway.scene_contracts import SceneGateway
from agent_runtime.scene.scene_session_registry import SceneSessionRegistry

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
    interaction_state: str
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
    interaction_state: ChatBubbleInteractionState = "none"
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
    interaction_state: ChatBubbleInteractionState | None = None
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
    max_image_inputs_per_request: int
    max_total_image_bytes_per_request: int
    max_single_input_file_bytes: int
    max_total_input_file_bytes_per_request: int


class FileInputStatsResponse(BaseModel):
    file_count: int = 0
    total_file_bytes: int = 0
    pdf_file_count: int = 0
    image_file_count: int = 0
    total_pdf_pages: int = 0
    total_image_bytes: int = 0
    total_input_file_bytes: int = 0


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
    client_session_id: str | None = None
    device_id: str | None = None
    client_kind: str = "unknown"


class AgentParentChildRelationResponse(BaseModel):
    relation_id: str
    task_id: str
    parent_agent_instance_id: str
    child_agent_instance_id: str
    created_at: str
    released_at: str | None = None
    release_reason: str | None = None
    released_by_agent_instance_id: str | None = None
    released: bool = False


class AttachSceneResponseModel(BaseModel):
    """attach scene 响应。

    mode：resumed | created | recreated。resumed 表示复用 Scene 长期 root agent instance；
    recreated 是旧设计兼容值，新设计默认不再因 task 完成而重建 root agent instance。
    """

    mode: str
    task: AgentRuntimeTaskResponse
    root_agent_instance: AgentInstanceResponse
    agent_instances: list[AgentInstanceResponse] = Field(default_factory=list)
    parent_child_relations: list[AgentParentChildRelationResponse] = Field(default_factory=list)
    communication_stream_id: str
    event_stream_url: str
    client_session_id: str | None = None
    active_client_count: int = 0


class DetachSceneBody(BaseModel):
    """detach scene 入参。"""

    workspace_path: str
    client_session_id: str | None = None


class DetachSceneResponseModel(BaseModel):
    """detach scene 响应。"""

    cancelled_task_count: int
    cancelled_instance_count: int


class CompleteCurrentSceneTaskBody(BaseModel):
    """用户主动完成 Scene 当前 Task 的请求。"""

    workspace_path: str


class CompleteCurrentSceneTaskResponseModel(BaseModel):
    """用户主动完成 Scene 当前 Task 的结构化业务结果。"""

    outcome: SceneTaskCompletionOutcome
    message: str
    scene_id: str
    task_id: str | None = None
    root_agent_instance_id: str | None = None


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

    @router.get("/tasks/{task_id}/agents/{agent_instance_id}/note")
    async def get_agent_instance_task_note(
        task_id: str,
        agent_instance_id: str,
        mission_id: str | None = None,
    ) -> dict[str, Any]:
        """获取指定 task_id 和 agent_instance_id 的 Task Note 结构化快照与 Markdown 视图。"""
        # 1. 校验 Agent 实例是否存在 (404 AGENT_INSTANCE_NOT_FOUND)
        cwd = ""
        try:
            instance = gateway.get_agent_instance(agent_instance_id)
            cwd = getattr(instance, "current_working_directory", "") or ""
        except (KeyError, Exception) as exc:
            logger.warning("获取 Task Note 时未找到 Agent 实例: %s", exc)
            raise HTTPException(
                status_code=404,
                detail=f"Agent 实例不存在: {agent_instance_id} (AGENT_INSTANCE_NOT_FOUND)",
            )

        # 2. 从底层服务构建快照与读取水位 (503 TASK_NOTE_STORAGE_UNAVAILABLE)
        from agent_runtime.task_note.task_note_store import TaskNoteStorageUnavailableError, get_task_note_store
        from agent_runtime.task_note.task_note_snapshot_service import get_task_note_snapshot_service
        from agent_runtime.task_note.task_note_markdown_formatter import format_task_note_to_markdown

        store = get_task_note_store()
        snapshot_service = get_task_note_snapshot_service()

        try:
            snapshot = snapshot_service.build_snapshot(
                task_id=task_id,
                agent_instance_id=agent_instance_id,
            )
            raw_wm = store.get_watermark(agent_instance_id)
        except TaskNoteStorageUnavailableError as exc:
            logger.warning("获取 Task Note 存储不可用: %s", exc)
            raise HTTPException(
                status_code=503,
                detail=f"Task Note 存储不可用: {exc} (TASK_NOTE_STORAGE_UNAVAILABLE)",
            )
        except Exception as exc:
            logger.error("获取 Task Note 异常: %s", exc)
            raise HTTPException(
                status_code=503,
                detail=f"Task Note 存储不可用: {exc} (TASK_NOTE_STORAGE_UNAVAILABLE)",
            )

        # 3. 组织水位字段
        if raw_wm and isinstance(raw_wm, dict):
            watermark = {
                "covered_until_seq": raw_wm.get("covered_until_seq", 0),
                "note_version": raw_wm.get("note_version", 0),
                "updated_at": raw_wm.get("updated_at"),
            }
        else:
            watermark = {
                "covered_until_seq": 0,
                "note_version": 0,
                "updated_at": None,
            }

        markdown = format_task_note_to_markdown(snapshot, current_working_directory=cwd)

        # 4. Phase 3 契约返回
        return {
            "task_id": task_id,
            "agent_instance_id": agent_instance_id,
            "mission_id": mission_id,
            "watermark": watermark,
            "observing": False,
            "snapshot": snapshot,
            "unbound": {
                "artifacts": [],
                "agent_relationships": [],
                "agent_messages": [],
                "remote_agent_messages": [],
            },
            "markdown": markdown,
        }

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
        request: Request,
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

        attach_request = AttachSceneRequest(
            workspace_path=body.workspace_path,
            fallback_llm_provider_id=fallback.llm_provider_id if fallback else None,
            fallback_llm_model_binding_id=fallback.llm_model_binding_id if fallback else None,
            fallback_llm_policy_group_id=fallback.llm_policy_group_id if fallback else None,
        )
        try:
            response = gateway.attach_scene(scene_id, attach_request)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except RuntimeError as e:
            raise HTTPException(status_code=501, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))

        # 注册 scene client session（心跳追踪）：同一 scene 可被多个前端设备同时 attach。
        client_session_id = body.client_session_id
        active_client_count = 0
        if session_registry is not None:
            auth_session = getattr(request.state, "runtime_auth_session", None)
            account = getattr(auth_session, "account", None)
            client_session_id = session_registry.open_session(
                scene_id,
                body.workspace_path,
                client_session_id=body.client_session_id,
                device_id=body.device_id,
                client_kind=body.client_kind,
                user_id=getattr(account, "account_id", None),
            )
            active_client_count = session_registry.active_client_count(scene_id)

        parent_child_relations = gateway.list_task_parent_child_relations(response.task.task_id)
        active_parent_child_relations = [
            relation
            for relation in parent_child_relations
            if not relation.is_released
        ]
        active_instance_ids = {response.instance.instance_id} | {
            relation.child_instance_id
            for relation in active_parent_child_relations
        }
        agent_instances = [
            instance
            for instance in gateway.list_task_agent_instances(response.task.task_id)
            if instance.instance_id in active_instance_ids
        ]
        return AttachSceneResponseModel(
            mode=response.mode,
            task=_task_response(response.task),
            root_agent_instance=await _instance_response(
                response.instance,
                conversation_repository,
                llm_registry_gateway,
            ),
            agent_instances=[
                await _instance_response(
                    instance,
                    conversation_repository,
                    llm_registry_gateway,
                )
                for instance in agent_instances
            ],
            parent_child_relations=[
                _relation_response(relation)
                for relation in active_parent_child_relations
            ],
            communication_stream_id=response.communication_stream_id,
            event_stream_url=response.event_stream_url,
            client_session_id=client_session_id,
            active_client_count=active_client_count,
        )

    @router.post("/scenes/{scene_id}/heartbeat", status_code=204)
    def scene_heartbeat(
        scene_id: str,
        client_session_id: str | None = Query(default=None),
    ) -> None:
        """前端定期发送的 scene 心跳，用于维持 session 活跃状态。

        前端应以 stale_timeout / 3 的间隔（默认约 40s）调用本接口。
        返回 204 表示心跳成功；404 表示 session 不存在，前端应重新 attach。
        """
        if session_registry is None:
            return
        if not session_registry.heartbeat(scene_id, client_session_id=client_session_id):
            raise HTTPException(
                status_code=404,
                detail=f"Scene session 不存在，请重新 attach: {scene_id}",
            )

    @router.post("/scenes/{scene_id}/detach", status_code=200)
    def detach_scene(
        scene_id: str,
        body: DetachSceneBody,
    ) -> DetachSceneResponseModel:
        """前端主动离开 scene，只释放当前客户端连接。

        多端模式下，普通移动端/桌面端关闭不能取消其他前端仍在观察或控制的任务；
        停止 scene 运行必须调用显式 stop 接口。
        """
        if session_registry is not None:
            session_registry.close_session(scene_id, body.client_session_id)
        return DetachSceneResponseModel(
            cancelled_task_count=0,
            cancelled_instance_count=0,
        )

    @router.post("/scenes/{scene_id}/stop", status_code=200)
    def stop_scene(
        scene_id: str,
        body: DetachSceneBody,
    ) -> DetachSceneResponseModel:
        """显式停止 scene 下未终态任务。

        该接口保留原 detach 的任务取消语义，用于用户明确执行“停止任务/关闭运行”。
        """
        request = DetachSceneRequest(workspace_path=body.workspace_path)
        response = gateway.detach_scene(scene_id, request)
        return DetachSceneResponseModel(
            cancelled_task_count=response.cancelled_task_count,
            cancelled_instance_count=response.cancelled_instance_count,
        )

    @router.post(
        "/scenes/{scene_id}/tasks/current/complete",
        response_model=CompleteCurrentSceneTaskResponseModel,
        responses={409: {"model": CompleteCurrentSceneTaskResponseModel}},
        status_code=200,
    )
    async def complete_current_scene_task(
        scene_id: str,
        body: CompleteCurrentSceneTaskBody,
    ) -> CompleteCurrentSceneTaskResponseModel | JSONResponse:
        """由用户直接成功完成 Scene 当前 Task，不经过 LLM 或伪造 tool call。"""
        if execution_engine is None:
            raise HTTPException(status_code=501, detail="Agent 执行引擎未启用。")
        if scene_gateway is None:
            raise HTTPException(status_code=501, detail="Scene 网关未启用，Task 完成入口不可用。")
        try:
            scene_gateway.get_scene(body.workspace_path, scene_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Scene 不存在: {scene_id}")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        try:
            result = await execution_engine.complete_current_scene_task(scene_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        response = _complete_current_scene_task_response(result)
        if result.outcome == "running_agents":
            return JSONResponse(status_code=409, content=response.model_dump(mode="json"))
        return response

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
                interaction_state=body.interaction_state,
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
                interaction_state=body.interaction_state,
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


@dataclass(frozen=True, slots=True)
class _LlmRuntimeSelection:
    llm_provider_id: str
    llm_model_binding_id: str
    llm_policy_group_id: str | None = None

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
    )


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


def _relation_response(relation: AgentParentChildRelation) -> AgentParentChildRelationResponse:
    return AgentParentChildRelationResponse(
        relation_id=relation.relation_id,
        task_id=relation.task_id,
        parent_agent_instance_id=relation.parent_instance_id,
        child_agent_instance_id=relation.child_instance_id,
        created_at=_datetime_text(relation.created_at),
        released_at=_datetime_text(relation.released_at) if relation.released_at else None,
        release_reason=relation.release_reason,
        released_by_agent_instance_id=relation.released_by_instance_id,
        released=relation.is_released,
    )


def _file_input_limits_response() -> FileInputLimitsResponse:
    return FileInputLimitsResponse(**file_input_limits_payload())


async def _next_request_file_stats(
    instance: AgentInstance,
    conversation_repository: ConversationRepository | None,
    file_resource_service: Any | None,
) -> FileInputStatsResponse:
    stats = await collect_next_request_file_input_stats(
        instance,
        conversation_repository,
        file_resource_service,
    )
    return FileInputStatsResponse(**file_input_stats_payload(stats))


def _complete_current_scene_task_response(
    result: SceneTaskCompletionResult,
) -> CompleteCurrentSceneTaskResponseModel:
    return CompleteCurrentSceneTaskResponseModel(
        outcome=result.outcome,
        message=result.message,
        scene_id=result.scene_id,
        task_id=result.task_id,
        root_agent_instance_id=result.root_agent_instance_id,
    )


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
    if llm_registry_gateway is None:
        return None
    if instance.llm_policy_group_id:
        try:
            policy = llm_registry_gateway.resolve_execution_policy(instance.llm_policy_group_id)
            first = policy.candidates[0] if policy.candidates else None
            if first is not None:
                return _context_usage_from_model(
                    provider_id=first.resolved_model.provider_id,
                    binding_id=first.resolved_model.binding_id,
                    model_id=first.resolved_model.model_id,
                    model_alias=first.resolved_model.model_alias,
                    context_window_max=first.resolved_model.context_window,
                )
        except (KeyError, ValueError):
            return None
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
        interaction_state=message.interaction_state,
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
