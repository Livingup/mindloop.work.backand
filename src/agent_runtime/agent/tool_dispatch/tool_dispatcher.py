# -*- coding: utf-8 -*-
"""Agent 执行循环内部工具调度器。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from agent_runtime.agent_definition.agent_definition import AgentDefinition, McpToolBinding
from agent_runtime.conversation_message.message.models import ToolResultPart, ToolUsePart
from agent_runtime.llm_api_protocol import (
    SDK_PLATFORM_ANTHROPIC,
    SDK_PLATFORM_GEMINI,
    SDK_PLATFORM_OPENAI,
)
from agent_runtime.llm_client.llm_request_models import LlmToolDefinition
from agent_runtime.mcp.mcp_client.north_gateway.mcp_client_contracts import McpClientGateway
from agent_runtime.tool.north_gateway.local_tool_contracts import (
    LocalToolGateway,
    ToolExecuteRequest,
)


AGENT_MCP_TOOL_NAME_PREFIX = "mcp__"


@dataclass(frozen=True, slots=True)
class ToolDispatchResult:
    """单次工具调用调度结果。"""

    # LLM 返回的工具名称。
    tool_name: str

    # 工具来源：local 或 mcp。
    source: str

    # 写回 conversation_message 的工具结果内容块。
    result_part: ToolResultPart


class AgentToolDispatcher:
    """Agent 内部工具调度器。

    调度器只暴露当前 AgentDefinition 明确允许的本地工具和 MCP 工具。LLM 请求前，
    build_llm_tools() 生成可传给对应 SDK 平台的工具 schema；LLM 返回工具调用后，
    dispatch_tool_call() 再次执行 allowlist 校验并调用本地工具或 MCP 工具。
    """

    def __init__(
        self,
        *,
        local_tool_gateway: LocalToolGateway,
        mcp_client_gateway: McpClientGateway | None = None,
    ) -> None:
        # 本地工具北向网关。用于读取本地工具 schema 和执行本地工具函数。
        self._local_tool_gateway = local_tool_gateway

        # MCP 客户端北向网关。AgentDefinition 声明 mcp_tools 时必须配置该网关。
        self._mcp_client_gateway = mcp_client_gateway

    async def build_llm_tools(
        self,
        *,
        agent_definition: AgentDefinition,
        sdk_platform: str,
    ) -> tuple[LlmToolDefinition, ...]:
        """根据 AgentDefinition 能力边界构建本轮 LLM 可见工具定义。"""
        tools: list[LlmToolDefinition] = []
        for tool_name in agent_definition.tool_ids:
            detail = self._local_tool_gateway.get_tool(tool_name)
            schema = detail.schemas.get(sdk_platform)
            if schema is None:
                raise ValueError(f"本地工具 {tool_name} 缺少 {sdk_platform} 平台 schema。")
            tools.append(LlmToolDefinition(name=tool_name, schema=schema, source="local"))

        if agent_definition.mcp_tools:
            if self._mcp_client_gateway is None:
                raise RuntimeError("当前 Agent 定义了 MCP 工具，但执行器未配置 McpClientGateway。")
            for binding in agent_definition.mcp_tools:
                details = await self._mcp_client_gateway.get_tool_detail(
                    binding.service_id,
                    list(binding.tool_ids),
                )
                detail_map = {detail.name: detail for detail in details}
                for tool_name in binding.tool_ids:
                    detail = detail_map.get(tool_name)
                    if detail is None:
                        raise KeyError(f"MCP 工具不存在: {binding.service_id}/{tool_name}")
                    exposed_name = build_mcp_exposed_tool_name(binding.service_id, tool_name)
                    schema = _build_mcp_tool_schema(
                        sdk_platform=sdk_platform,
                        exposed_name=exposed_name,
                        description=detail.description,
                        input_schema=detail.input_schema,
                    )
                    tools.append(
                        LlmToolDefinition(
                            name=exposed_name,
                            schema=schema,
                            source="mcp",
                            mcp_service_id=binding.service_id,
                            mcp_tool_name=tool_name,
                        )
                    )
        return tuple(tools)

    async def dispatch_tool_call(
        self,
        *,
        agent_definition: AgentDefinition,
        tool_use: ToolUsePart,
    ) -> ToolDispatchResult:
        """执行 LLM 返回的一次工具调用，并返回可写入会话的 ToolResultPart。"""
        if tool_use.name.startswith(AGENT_MCP_TOOL_NAME_PREFIX):
            return await self._dispatch_mcp_tool(agent_definition, tool_use)
        return await self._dispatch_local_tool(agent_definition, tool_use)

    async def _dispatch_local_tool(
        self,
        agent_definition: AgentDefinition,
        tool_use: ToolUsePart,
    ) -> ToolDispatchResult:
        """执行本地工具调用。"""
        if tool_use.name not in agent_definition.tool_ids:
            raise PermissionError(f"Agent {agent_definition.agent_id} 无权调用本地工具: {tool_use.name}")
        params = _ensure_dict_arguments(tool_use.input)
        result = await self._local_tool_gateway.execute_tool(
            ToolExecuteRequest(tool_name=tool_use.name, params=params),
        )
        return ToolDispatchResult(
            tool_name=tool_use.name,
            source="local",
            result_part=ToolResultPart(
                tool_use_ref=_tool_use_ref(tool_use),
                output=result.result,
            ),
        )

    async def _dispatch_mcp_tool(
        self,
        agent_definition: AgentDefinition,
        tool_use: ToolUsePart,
    ) -> ToolDispatchResult:
        """执行 MCP 工具调用。"""
        if self._mcp_client_gateway is None:
            raise RuntimeError("当前执行器未配置 McpClientGateway，不能执行 MCP 工具。")
        service_id, tool_name = parse_mcp_exposed_tool_name(tool_use.name)
        _ensure_mcp_tool_allowed(agent_definition.mcp_tools, service_id, tool_name)
        params = _ensure_dict_arguments(tool_use.input)
        result = await self._mcp_client_gateway.call_tool(service_id, tool_name, params)
        output: Any = result.structured_content if result.structured_content is not None else result.content
        if result.is_error:
            output = {"is_error": True, "content": result.content, "structured_content": result.structured_content}
        return ToolDispatchResult(
            tool_name=tool_use.name,
            source="mcp",
            result_part=ToolResultPart(
                tool_use_ref=_tool_use_ref(tool_use),
                output=output,
            ),
        )


def build_mcp_exposed_tool_name(service_id: str, tool_name: str) -> str:
    """构造暴露给 LLM 的 MCP 工具名，避免和本地工具重名。"""
    return f"{AGENT_MCP_TOOL_NAME_PREFIX}{service_id}__{tool_name}"


def parse_mcp_exposed_tool_name(exposed_name: str) -> tuple[str, str]:
    """从 LLM 返回的 MCP 暴露工具名还原 service_id 与 tool_name。"""
    if not exposed_name.startswith(AGENT_MCP_TOOL_NAME_PREFIX):
        raise ValueError(f"不是 MCP 暴露工具名: {exposed_name}")
    payload = exposed_name[len(AGENT_MCP_TOOL_NAME_PREFIX):]
    service_id, separator, tool_name = payload.partition("__")
    if not separator or not service_id or not tool_name:
        raise ValueError(f"MCP 暴露工具名格式错误: {exposed_name}")
    return service_id, tool_name


def _build_mcp_tool_schema(
    *,
    sdk_platform: str,
    exposed_name: str,
    description: str | None,
    input_schema: dict[str, Any],
) -> dict[str, Any]:
    """把 MCP inputSchema 转为目标 SDK 平台工具 schema。"""
    parameters = input_schema or {"type": "object", "properties": {}}
    if sdk_platform == SDK_PLATFORM_OPENAI:
        return {
            "type": "function",
            "function": {
                "name": exposed_name,
                "description": description or "",
                "parameters": parameters,
            },
        }
    if sdk_platform == SDK_PLATFORM_ANTHROPIC:
        return {
            "name": exposed_name,
            "description": description or "",
            "input_schema": parameters,
        }
    if sdk_platform == SDK_PLATFORM_GEMINI:
        return {
            "name": exposed_name,
            "description": description or "",
            "parameters": parameters,
        }
    raise ValueError(f"当前工具调度器不支持 sdk_platform={sdk_platform} 的工具 schema。")


def _ensure_mcp_tool_allowed(bindings: tuple[McpToolBinding, ...], service_id: str, tool_name: str) -> None:
    """校验当前 AgentDefinition 是否允许调用指定 MCP 工具。"""
    for binding in bindings:
        if binding.service_id == service_id and tool_name in binding.tool_ids:
            return
    raise PermissionError(f"当前 Agent 无权调用 MCP 工具: {service_id}/{tool_name}")


def _ensure_dict_arguments(value: Any) -> dict[str, Any]:
    """把 LLM 返回的工具参数规范为字典。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"input": value}
        if isinstance(parsed, dict):
            return parsed
        return {"input": parsed}
    if value is None:
        return {}
    return {"input": value}


def _tool_use_ref(tool_use: ToolUsePart) -> str:
    """确定工具结果要关联的工具调用引用。"""
    return tool_use.tool_use_id or tool_use.name
