from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from agent_runtime.agent.agent_instance import AgentInstance
from agent_runtime.agent.agent_execution_engine import (
    AgentExecutionEngine,
    AgentExecutionResult,
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
from agent_runtime.llm_model_registry.llm_model_registry_service import (
    ResolvedLlmExecutionPolicy,
)
from agent_runtime.llm_model_registry.north_gateway.llm_model_registry_contracts import (
    LlmModelRegistryGateway,
)
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
    created_at: str
    updated_at: str


class CreateRootAgentInstanceResponse(BaseModel):
    task: AgentRuntimeTaskResponse
    root_agent_instance: AgentInstanceResponse


class SendAgentInstanceMessageBody(BaseModel):
    user_message: str
    client_message_id: str | None = None
    render_options: dict[str, Any] = Field(default_factory=dict)
    request_overrides: dict[str, Any] = Field(default_factory=dict)
    communication_stream_id: str | None = None


class SwitchAgentInstanceLlmPolicyBody(BaseModel):
    llm_policy_group_id: str


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
    root_agent_instance: AgentInstanceResponse | None = None
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




def create_agent_runtime_router(
    gateway: AgentRuntimeGateway,
    llm_registry_gateway: LlmModelRegistryGateway | None = None,
    execution_engine: AgentExecutionEngine | None = None,
    agent_definition_gateway: AgentDefinitionNorthGateway | None = None,
    scene_gateway: SceneGateway | None = None,
    session_registry: SceneSessionRegistry | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/root-instances",
        response_model=CreateRootAgentInstanceResponse,
        status_code=201,
    )
    def create_root_agent_instance(
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
            root_agent_instance=_instance_response(instance),
        )

    @router.get("/tasks/{task_id}", response_model=AgentRuntimeTaskResponse)
    def get_task(task_id: str) -> AgentRuntimeTaskResponse:
        try:
            task = gateway.get_task(task_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Agent 任务不存在: {task_id}")
        return _task_response(task)

    @router.get("/instances/{instance_id}", response_model=AgentInstanceResponse)
    def get_agent_instance(instance_id: str) -> AgentInstanceResponse:
        try:
            instance = gateway.get_agent_instance(instance_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Agent 实例不存在: {instance_id}")
        return _instance_response(instance)

    @router.patch("/instances/{instance_id}/llm-policy", response_model=AgentInstanceResponse)
    def switch_agent_instance_llm_policy(
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
        return _instance_response(updated)

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
        if not body.user_message.strip():
            raise HTTPException(status_code=400, detail="user_message 不能为空。")
        try:
            result = await execution_engine.submit_agent_instance_message(
                agent_instance_id=instance_id,
                user_text=body.user_message,
                render_options=body.render_options,
                request_overrides=body.request_overrides,
                communication_stream_id=body.communication_stream_id,
                client_message_id=body.client_message_id,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Agent 实例不存在: {instance_id}")
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return _submission_response(result)

    @router.post(
        "/scenes/{scene_id}/attach",
        response_model=AttachSceneResponseModel,
        status_code=200,
    )
    def attach_scene(
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
            root_agent_instance=_instance_response(response.instance),
            communication_stream_id=response.communication_stream_id,
            event_stream_url=response.event_stream_url,
        )

    @router.post("/scenes/{scene_id}/heartbeat", status_code=204)
    def scene_heartbeat(scene_id: str) -> None:
        """前端定期发送的 scene 心跳，用于维持 session 活跃状态。

        前端应以 stale_timeout / 3 的间隔（默认约 10s）调用本接口。
        返回 204 表示心跳成功；404 表示 session 不存在，前端应重新 attach。
        """
        if session_registry is None:
            return
        if not session_registry.heartbeat(scene_id):
            raise HTTPException(
                status_code=404,
                detail=f"Scene session 不存在，请重新 attach: {scene_id}",
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

    return router


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


def _instance_response(instance: AgentInstance) -> AgentInstanceResponse:
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
        created_at=_datetime_text(instance.created_at),
        updated_at=_datetime_text(instance.updated_at),
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


def _submission_response(result: AgentMessageSubmissionResult) -> AgentMessageSubmissionResponse:
    return AgentMessageSubmissionResponse(
        status=result.status,
        task_id=result.task_id,
        agent_instance_id=result.instance_id,
        conversation_id=result.conversation_id,
        communication_stream_id=result.communication_stream_id,
        event_stream_url=f"/api/communication/streams/{result.communication_stream_id}/events",
        root_agent_instance=_instance_response(result.instance),
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
