# -*- coding: utf-8 -*-
"""Agent 定义 REST API 接入适配。

提供 FastAPI 路由，支持 Agent 静态模板定义的增删改查和启用禁用。
路由挂载到主应用时建议使用 prefix="/api/agents"。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from agent_runtime.agent_definition.agent_definition import (
    AgentDefinition,
    McpToolBinding,
    SystemPromptSegment,
)
from agent_runtime.agent_definition.north_gateway.agent_definition_contracts import (
    AgentDefinitionCreateRequest,
    AgentDefinitionNorthGateway,
    AgentDefinitionUpdateRequest,
)


# ── 请求 / 响应 Pydantic 模型 ───────────────────────────────────


class SystemPromptSegmentBody(BaseModel):
    """系统提示词片段请求体。

    text 与 template_id 必须且只能填写一个。该约束由领域实体 SystemPromptSegment 统一校验。
    """

    text: str | None = None
    template_id: str | None = None


class McpToolBindingBody(BaseModel):
    """MCP 工具绑定请求体。"""

    service_id: str
    tool_ids: list[str] = Field(default_factory=list)


class McpToolIdsBody(BaseModel):
    """MCP 服务工具 ID 子资源请求体。"""

    tool_ids: list[str] = Field(default_factory=list)


class RelationIdBody(BaseModel):
    """通用 ID 关系请求体。"""

    id: str


class IdListBody(BaseModel):
    """关系重排序请求体。"""

    ids: list[str] = Field(default_factory=list)


class IndexListBody(BaseModel):
    """系统提示词片段重排序请求体。"""

    indices: list[int] = Field(default_factory=list)


class AgentDefinitionResponse(BaseModel):
    """Agent 定义响应体。"""

    agent_id: str
    name: str | None = None
    description: str | None = None
    group: str = "default"
    system_prompt: list[dict[str, str]] = Field(default_factory=list)
    tool_ids: list[str] = Field(default_factory=list)
    mcp_tools: list[dict[str, Any]] = Field(default_factory=list)
    skill_ids: list[str] = Field(default_factory=list)
    available_sub_agent_ids: list[str] = Field(default_factory=list)
    default_llm_policy_group_id: str | None = None
    allowed_llm_policy_group_ids: list[str] = Field(default_factory=list)
    builtin_background_agent: bool = False
    disabled: bool = False


class AgentDefinitionCreateBody(BaseModel):
    """创建 Agent 定义请求体。"""

    agent_id: str
    name: str | None = None
    description: str | None = None
    group: str = "default"
    system_prompt: list[SystemPromptSegmentBody] = Field(default_factory=list)
    tool_ids: list[str] = Field(default_factory=list)
    mcp_tools: list[McpToolBindingBody] = Field(default_factory=list)
    skill_ids: list[str] = Field(default_factory=list)
    available_sub_agent_ids: list[str] = Field(default_factory=list)
    default_llm_policy_group_id: str | None = None
    allowed_llm_policy_group_ids: list[str] = Field(default_factory=list)
    builtin_background_agent: bool = False
    disabled: bool = False


class AgentDefinitionUpdateBody(BaseModel):
    """更新 Agent 定义请求体（部分更新）。

    字段为 None 表示不修改；数组字段传空数组表示清空对应能力列表。
    """

    name: str | None = None
    description: str | None = None
    group: str | None = None
    system_prompt: list[SystemPromptSegmentBody] | None = None
    tool_ids: list[str] | None = None
    mcp_tools: list[McpToolBindingBody] | None = None
    skill_ids: list[str] | None = None
    available_sub_agent_ids: list[str] | None = None
    default_llm_policy_group_id: str | None = None
    allowed_llm_policy_group_ids: list[str] | None = None
    builtin_background_agent: bool | None = None
    disabled: bool | None = None


class StatusResponse(BaseModel):
    """操作状态响应。"""

    status: str
    message: str


# ── 路由工厂 ─────────────────────────────────────────────────────


def create_agent_definition_router(
    gateway: AgentDefinitionNorthGateway,
) -> APIRouter:
    """根据网关实例创建 Agent 定义 REST API 路由。

    Args:
        gateway: Agent 定义北向网关实例。

    Returns:
        FastAPI APIRouter，挂载时建议使用 prefix="/api/agents"。
    """
    router = APIRouter()

    @router.get("", response_model=list[dict[str, Any]])
    def list_agent_definitions(
        group: str | None = None,
        include_disabled: bool = True,
        summary: bool = False,
    ) -> list[dict[str, Any]]:
        """列出 Agent 定义；summary=true 时仅返回低噪声摘要。"""
        definitions = gateway.list_agent_definitions(
            group=group,
            include_disabled=include_disabled,
        )
        if summary:
            return [
                {
                    "agent_id": item.agent_id,
                    "name": item.name,
                    "description": item.description,
                    "group": item.group,
                    "disabled": item.disabled,
                    "builtin_background_agent": item.builtin_background_agent,
                    "default_llm_policy_group_id": item.default_llm_policy_group_id,
                }
                for item in definitions
            ]
        return [_definition_to_response(item).model_dump() for item in definitions]

    @router.get("/{agent_id}", response_model=AgentDefinitionResponse)
    def get_agent_definition(agent_id: str) -> AgentDefinitionResponse:
        """根据 agent_id 获取 Agent 定义。"""
        try:
            definition = gateway.get_agent_definition(agent_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Agent 定义不存在: {agent_id}")
        return _definition_to_response(definition)

    @router.post("", response_model=AgentDefinitionResponse, status_code=201)
    def create_agent_definition(
        body: AgentDefinitionCreateBody,
    ) -> AgentDefinitionResponse:
        """创建 Agent 定义。"""
        try:
            definition = gateway.create_agent_definition(_body_to_create_request(body))
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return _definition_to_response(definition)

    @router.put("/{agent_id}", response_model=AgentDefinitionResponse)
    def update_agent_definition(
        agent_id: str,
        body: AgentDefinitionUpdateBody,
    ) -> AgentDefinitionResponse:
        """部分更新 Agent 定义。"""
        try:
            definition = gateway.update_agent_definition(
                agent_id,
                _body_to_update_request(body),
            )
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Agent 定义不存在: {agent_id}")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return _definition_to_response(definition)

    @router.patch("/{agent_id}", response_model=AgentDefinitionResponse)
    def patch_agent_definition(
        agent_id: str,
        body: AgentDefinitionUpdateBody,
    ) -> AgentDefinitionResponse:
        """部分更新 Agent 定义；语义与 PUT 保持一致。"""
        return update_agent_definition(agent_id, body)

    @router.get("/{agent_id}/system-prompt", response_model=list[dict[str, str]])
    def list_system_prompt_segments(agent_id: str) -> list[dict[str, str]]:
        """列出 Agent 系统提示词片段。"""
        definition = _get_definition_or_404(gateway, agent_id)
        return [item.to_dict() for item in definition.system_prompt]

    @router.post("/{agent_id}/system-prompt", response_model=AgentDefinitionResponse, status_code=201)
    def append_system_prompt_segment(
        agent_id: str,
        body: SystemPromptSegmentBody,
    ) -> AgentDefinitionResponse:
        """追加 Agent 系统提示词片段。"""
        definition = _get_definition_or_404(gateway, agent_id)
        prompt = definition.system_prompt + (_body_to_prompt_segment(body),)
        return _update_definition_or_400(gateway, agent_id, AgentDefinitionUpdateRequest(system_prompt=prompt))

    @router.put("/{agent_id}/system-prompt/{index}", response_model=AgentDefinitionResponse)
    def replace_system_prompt_segment(
        agent_id: str,
        index: int,
        body: SystemPromptSegmentBody,
    ) -> AgentDefinitionResponse:
        """按下标替换 Agent 系统提示词片段。"""
        definition = _get_definition_or_404(gateway, agent_id)
        prompt = list(definition.system_prompt)
        _ensure_index(index, len(prompt), "system_prompt")
        prompt[index] = _body_to_prompt_segment(body)
        return _update_definition_or_400(gateway, agent_id, AgentDefinitionUpdateRequest(system_prompt=tuple(prompt)))

    @router.delete("/{agent_id}/system-prompt/{index}", response_model=AgentDefinitionResponse)
    def delete_system_prompt_segment(agent_id: str, index: int) -> AgentDefinitionResponse:
        """按下标删除 Agent 系统提示词片段。"""
        definition = _get_definition_or_404(gateway, agent_id)
        prompt = list(definition.system_prompt)
        _ensure_index(index, len(prompt), "system_prompt")
        del prompt[index]
        return _update_definition_or_400(gateway, agent_id, AgentDefinitionUpdateRequest(system_prompt=tuple(prompt)))

    @router.post("/{agent_id}/system-prompt/reorder", response_model=AgentDefinitionResponse)
    def reorder_system_prompt_segments(
        agent_id: str,
        body: IndexListBody,
    ) -> AgentDefinitionResponse:
        """按完整下标列表重排 Agent 系统提示词片段。"""
        definition = _get_definition_or_404(gateway, agent_id)
        prompt = _reorder_by_indices(definition.system_prompt, body.indices, "system_prompt")
        return _update_definition_or_400(gateway, agent_id, AgentDefinitionUpdateRequest(system_prompt=prompt))

    @router.get("/{agent_id}/mcp-tools", response_model=list[dict[str, Any]])
    def list_agent_mcp_tools(agent_id: str) -> list[dict[str, Any]]:
        """列出 Agent MCP 工具绑定关系。"""
        definition = _get_definition_or_404(gateway, agent_id)
        return [item.to_dict() for item in definition.mcp_tools]

    @router.post("/{agent_id}/mcp-tools", response_model=AgentDefinitionResponse, status_code=201)
    def add_agent_mcp_tool_binding(
        agent_id: str,
        body: McpToolBindingBody,
    ) -> AgentDefinitionResponse:
        """新增 Agent MCP 工具绑定关系。"""
        definition = _get_definition_or_404(gateway, agent_id)
        if any(item.service_id == body.service_id for item in definition.mcp_tools):
            raise HTTPException(status_code=409, detail=f"MCP 服务绑定已存在: {body.service_id}")
        bindings = definition.mcp_tools + (_body_to_mcp_binding(body),)
        return _update_definition_or_400(gateway, agent_id, AgentDefinitionUpdateRequest(mcp_tools=bindings))

    @router.put("/{agent_id}/mcp-tools/{service_id}", response_model=AgentDefinitionResponse)
    def set_agent_mcp_tool_binding(
        agent_id: str,
        service_id: str,
        body: McpToolIdsBody,
    ) -> AgentDefinitionResponse:
        """新增或替换指定 MCP 服务的工具绑定。"""
        definition = _get_definition_or_404(gateway, agent_id)
        replacement = McpToolBinding(service_id=service_id, tool_ids=tuple(body.tool_ids))
        bindings = tuple(item for item in definition.mcp_tools if item.service_id != service_id) + (replacement,)
        return _update_definition_or_400(gateway, agent_id, AgentDefinitionUpdateRequest(mcp_tools=bindings))

    @router.delete("/{agent_id}/mcp-tools/{service_id}", response_model=AgentDefinitionResponse)
    def delete_agent_mcp_tool_binding(agent_id: str, service_id: str) -> AgentDefinitionResponse:
        """删除指定 MCP 服务的工具绑定关系。"""
        definition = _get_definition_or_404(gateway, agent_id)
        bindings = tuple(item for item in definition.mcp_tools if item.service_id != service_id)
        if len(bindings) == len(definition.mcp_tools):
            raise HTTPException(status_code=404, detail=f"MCP 服务绑定不存在: {service_id}")
        return _update_definition_or_400(gateway, agent_id, AgentDefinitionUpdateRequest(mcp_tools=bindings))

    @router.post("/{agent_id}/mcp-tools/reorder", response_model=AgentDefinitionResponse)
    def reorder_agent_mcp_tool_bindings(
        agent_id: str,
        body: IdListBody,
    ) -> AgentDefinitionResponse:
        """按完整 service_id 列表重排 Agent MCP 工具绑定关系。"""
        definition = _get_definition_or_404(gateway, agent_id)
        bindings = _reorder_by_ids(definition.mcp_tools, body.ids, "mcp_tools.service_id", "service_id")
        return _update_definition_or_400(gateway, agent_id, AgentDefinitionUpdateRequest(mcp_tools=bindings))

    @router.get("/{agent_id}/skills", response_model=list[str])
    def list_agent_skills(agent_id: str) -> list[str]:
        """列出 Agent Skill 关系。"""
        definition = _get_definition_or_404(gateway, agent_id)
        return list(definition.skill_ids)

    @router.post("/{agent_id}/skills", response_model=AgentDefinitionResponse, status_code=201)
    def add_agent_skill(agent_id: str, body: RelationIdBody) -> AgentDefinitionResponse:
        """新增 Agent Skill 关系。"""
        definition = _get_definition_or_404(gateway, agent_id)
        if body.id in definition.skill_ids:
            raise HTTPException(status_code=409, detail=f"Skill 关系已存在: {body.id}")
        skill_ids = definition.skill_ids + (body.id,)
        return _update_definition_or_400(gateway, agent_id, AgentDefinitionUpdateRequest(skill_ids=skill_ids))

    @router.delete("/{agent_id}/skills/{skill_id}", response_model=AgentDefinitionResponse)
    def delete_agent_skill(agent_id: str, skill_id: str) -> AgentDefinitionResponse:
        """删除 Agent Skill 关系。"""
        definition = _get_definition_or_404(gateway, agent_id)
        skill_ids = tuple(item for item in definition.skill_ids if item != skill_id)
        if len(skill_ids) == len(definition.skill_ids):
            raise HTTPException(status_code=404, detail=f"Skill 关系不存在: {skill_id}")
        return _update_definition_or_400(gateway, agent_id, AgentDefinitionUpdateRequest(skill_ids=skill_ids))

    @router.post("/{agent_id}/skills/reorder", response_model=AgentDefinitionResponse)
    def reorder_agent_skills(agent_id: str, body: IdListBody) -> AgentDefinitionResponse:
        """按完整 Skill ID 列表重排 Agent Skill 关系。"""
        definition = _get_definition_or_404(gateway, agent_id)
        skill_ids = _reorder_plain_ids(definition.skill_ids, body.ids, "skill_ids")
        return _update_definition_or_400(gateway, agent_id, AgentDefinitionUpdateRequest(skill_ids=skill_ids))

    @router.delete("/{agent_id}", status_code=204)
    def delete_agent_definition(agent_id: str) -> None:
        """删除 Agent 定义。不存在时静默返回 204。"""
        gateway.delete_agent_definition(agent_id)

    @router.post("/{agent_id}/enable", response_model=AgentDefinitionResponse)
    def enable_agent_definition(agent_id: str) -> AgentDefinitionResponse:
        """启用 Agent 定义。"""
        try:
            definition = gateway.enable_agent_definition(agent_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Agent 定义不存在: {agent_id}")
        return _definition_to_response(definition)

    @router.post("/{agent_id}/disable", response_model=AgentDefinitionResponse)
    def disable_agent_definition(agent_id: str) -> AgentDefinitionResponse:
        """禁用 Agent 定义。"""
        try:
            definition = gateway.disable_agent_definition(agent_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Agent 定义不存在: {agent_id}")
        return _definition_to_response(definition)

    return router


def _definition_to_response(definition: AgentDefinition) -> AgentDefinitionResponse:
    """将领域实体转换为 REST 响应模型。"""
    return AgentDefinitionResponse(**definition.to_dict())


def _get_definition_or_404(
    gateway: AgentDefinitionNorthGateway,
    agent_id: str,
) -> AgentDefinition:
    try:
        return gateway.get_agent_definition(agent_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Agent 定义不存在: {agent_id}")


def _update_definition_or_400(
    gateway: AgentDefinitionNorthGateway,
    agent_id: str,
    request: AgentDefinitionUpdateRequest,
) -> AgentDefinitionResponse:
    try:
        definition = gateway.update_agent_definition(agent_id, request)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Agent 定义不存在: {agent_id}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _definition_to_response(definition)


def _body_to_prompt_segment(body: SystemPromptSegmentBody) -> SystemPromptSegment:
    return SystemPromptSegment(text=body.text, template_id=body.template_id)


def _body_to_mcp_binding(body: McpToolBindingBody) -> McpToolBinding:
    return McpToolBinding(service_id=body.service_id, tool_ids=tuple(body.tool_ids))


def _ensure_index(index: int, length: int, field_name: str) -> None:
    if index < 0 or index >= length:
        raise HTTPException(status_code=404, detail=f"{field_name} 下标不存在: {index}")


def _reorder_by_indices(
    items: tuple[Any, ...],
    indices: list[int],
    field_name: str,
) -> tuple[Any, ...]:
    if len(indices) != len(set(indices)):
        raise HTTPException(status_code=400, detail=f"{field_name} 重排序下标不能重复。")
    expected = set(range(len(items)))
    requested = set(indices)
    if requested != expected:
        raise HTTPException(status_code=400, detail=f"{field_name} 重排序下标必须完整覆盖当前片段。")
    return tuple(items[index] for index in indices)


def _reorder_plain_ids(
    current_ids: tuple[str, ...],
    requested_ids: list[str],
    field_name: str,
) -> tuple[str, ...]:
    if len(requested_ids) != len(set(requested_ids)):
        raise HTTPException(status_code=400, detail=f"{field_name} 重排序 ID 不能重复。")
    if set(requested_ids) != set(current_ids):
        raise HTTPException(status_code=400, detail=f"{field_name} 重排序 ID 必须完整覆盖当前关系。")
    return tuple(requested_ids)


def _reorder_by_ids(
    items: tuple[Any, ...],
    requested_ids: list[str],
    field_name: str,
    id_attribute: str,
) -> tuple[Any, ...]:
    if len(requested_ids) != len(set(requested_ids)):
        raise HTTPException(status_code=400, detail=f"{field_name} 重排序 ID 不能重复。")
    by_id = {str(getattr(item, id_attribute)): item for item in items}
    if set(requested_ids) != set(by_id):
        raise HTTPException(status_code=400, detail=f"{field_name} 重排序 ID 必须完整覆盖当前关系。")
    return tuple(by_id[item_id] for item_id in requested_ids)


def _body_to_create_request(
    body: AgentDefinitionCreateBody,
) -> AgentDefinitionCreateRequest:
    """将 REST 创建请求体转换为北向创建请求。"""
    return AgentDefinitionCreateRequest(
        agent_id=body.agent_id,
        name=body.name,
        description=body.description,
        group=body.group,
        system_prompt=tuple(
            SystemPromptSegment(text=item.text, template_id=item.template_id)
            for item in body.system_prompt
        ),
        tool_ids=tuple(body.tool_ids),
        mcp_tools=tuple(
            McpToolBinding(service_id=item.service_id, tool_ids=tuple(item.tool_ids))
            for item in body.mcp_tools
        ),
        skill_ids=tuple(body.skill_ids),
        available_sub_agent_ids=tuple(body.available_sub_agent_ids),
        default_llm_policy_group_id=body.default_llm_policy_group_id,
        allowed_llm_policy_group_ids=tuple(body.allowed_llm_policy_group_ids),
        builtin_background_agent=body.builtin_background_agent,
        disabled=body.disabled,
    )


def _body_to_update_request(
    body: AgentDefinitionUpdateBody,
) -> AgentDefinitionUpdateRequest:
    """将 REST 更新请求体转换为北向更新请求。"""
    body_fields_set = getattr(body, "model_fields_set", getattr(body, "__fields_set__", set()))
    return AgentDefinitionUpdateRequest(
        name=body.name,
        description=body.description,
        group=body.group,
        system_prompt=(
            tuple(SystemPromptSegment(text=item.text, template_id=item.template_id) for item in body.system_prompt)
            if body.system_prompt is not None else None
        ),
        tool_ids=tuple(body.tool_ids) if body.tool_ids is not None else None,
        mcp_tools=(
            tuple(
                McpToolBinding(service_id=item.service_id, tool_ids=tuple(item.tool_ids))
                for item in body.mcp_tools
            )
            if body.mcp_tools is not None else None
        ),
        skill_ids=tuple(body.skill_ids) if body.skill_ids is not None else None,
        available_sub_agent_ids=(
            tuple(body.available_sub_agent_ids)
            if body.available_sub_agent_ids is not None else None
        ),
        default_llm_policy_group_id=body.default_llm_policy_group_id,
        update_default_llm_policy_group_id="default_llm_policy_group_id" in body_fields_set,
        allowed_llm_policy_group_ids=(
            tuple(body.allowed_llm_policy_group_ids)
            if body.allowed_llm_policy_group_ids is not None else None
        ),
        builtin_background_agent=body.builtin_background_agent,
        disabled=body.disabled,
    )
