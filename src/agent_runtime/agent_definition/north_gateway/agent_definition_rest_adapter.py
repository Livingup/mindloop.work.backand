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

    @router.get("", response_model=list[AgentDefinitionResponse])
    def list_agent_definitions(
        group: str | None = None,
        include_disabled: bool = True,
    ) -> list[AgentDefinitionResponse]:
        """列出 Agent 定义，可按分组和禁用状态筛选。"""
        definitions = gateway.list_agent_definitions(
            group=group,
            include_disabled=include_disabled,
        )
        return [_definition_to_response(item) for item in definitions]

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
