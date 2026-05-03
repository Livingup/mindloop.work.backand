# -*- coding: utf-8 -*-
"""MCP 元工具定义和实现。

将 MCP 客户端能力封装为 5 个本地工具，通过 @tool 装饰器注册。
Agent 在对话中通过这些工具自主发现和调用 MCP 服务。

注意：元工具依赖 McpClientGateway 实例，必须先调用 configure() 注入依赖后才能使用。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from agent_runtime.tool.tool_decorator import tool
from agent_runtime.mcp.mcp_client.north_gateway.mcp_client_contracts import (
    McpClientGateway,
)

# 模块级 gateway 引用，由 configure() 注入
_gateway: McpClientGateway | None = None


def configure(gateway: McpClientGateway) -> None:
    """注入 McpClientGateway 依赖。

    必须在 Agent 启动前调用，否则元工具执行时会报错。

    Args:
        gateway: MCP 客户端北向网关实例。
    """
    global _gateway
    _gateway = gateway


def _get_gateway() -> McpClientGateway:
    """获取已配置的 gateway 实例。"""
    if _gateway is None:
        raise RuntimeError(
            "MCP 元工具未初始化：请先调用 mcp_meta_tools.configure(gateway)"
        )
    return _gateway


def _tool_error(message: str) -> str:
    return json.dumps({"is_error": True, "content": message}, ensure_ascii=False)


# ══════════════════════════════════════════
# 5 个 MCP 元工具
# ══════════════════════════════════════════


@tool(
    name="mcp_get_service_detail",
    description="获取 MCP 服务的能力信息和使用指南（instructions）。用于了解服务支持哪些能力、获取跨工具的使用策略。",
    group="mcp",
    wiki="查询指定 MCP 服务的 capabilities（是否支持 tools/resources）和 instructions（使用指南）。"
    "在调用 MCP 工具前建议先用此工具了解服务的使用策略。",
)
async def mcp_get_service_detail(service_id: str) -> str:
    """获取 MCP 服务的能力信息和使用指南。

    Args:
        service_id: MCP 服务唯一标识。

    Returns:
        JSON 格式的服务能力详情。
    """
    try:
        gw = _get_gateway()
        detail = await gw.get_service_detail(service_id)
    except (Exception, asyncio.CancelledError) as e:
        return _tool_error(f"MCP 服务详情获取失败: {e}")
    return json.dumps(
        {
            "supports_tools": detail.supports_tools,
            "supports_resources": detail.supports_resources,
            "server_name": detail.server_name,
            "server_version": detail.server_version,
            "instructions": detail.instructions,
        },
        ensure_ascii=False,
    )


@tool(
    name="mcp_list_resources",
    description="列出 MCP 服务的可用资源（URI、名称、MIME 类型）。用于发现服务提供的上下文数据。",
    group="mcp",
    wiki="列出指定 MCP 服务暴露的所有资源。"
    "返回资源列表，每个资源包含 uri、name、description、mime_type。",
)
async def mcp_list_resources(service_id: str) -> str:
    """列出 MCP 服务的可用资源。

    Args:
        service_id: MCP 服务唯一标识。

    Returns:
        JSON 格式的资源列表。
    """
    try:
        gw = _get_gateway()
        resources = await gw.list_resources(service_id)
    except (Exception, asyncio.CancelledError) as e:
        return _tool_error(f"MCP 资源列表获取失败: {e}")
    return json.dumps(
        [
            {
                "uri": r.uri,
                "name": r.name,
                "description": r.description,
                "mime_type": r.mime_type,
            }
            for r in resources
        ],
        ensure_ascii=False,
    )


@tool(
    name="mcp_read_resource",
    description="读取 MCP 服务的指定资源内容。需要先用 mcp_list_resources 获取资源 URI。",
    group="mcp",
    wiki="读取指定 MCP 服务上某个资源的具体内容。"
    "需要提供 service_id 和资源的 URI（从 mcp_list_resources 获取）。",
)
async def mcp_read_resource(service_id: str, uri: str) -> str:
    """读取 MCP 服务的指定资源内容。

    Args:
        service_id: MCP 服务唯一标识。
        uri: 资源 URI（从 mcp_list_resources 获取）。

    Returns:
        JSON 格式的资源内容。
    """
    try:
        gw = _get_gateway()
        content = await gw.read_resource(service_id, uri)
    except (Exception, asyncio.CancelledError) as e:
        return _tool_error(f"MCP 资源读取失败: {e}")
    result: dict[str, Any] = {
        "uri": content.uri,
        "mime_type": content.mime_type,
    }
    if content.text is not None:
        result["text"] = content.text
    if content.blob is not None:
        result["blob"] = content.blob
    return json.dumps(result, ensure_ascii=False)


@tool(
    name="mcp_get_tool_detail",
    description="获取 MCP 服务上指定工具的参数定义（inputSchema）。在调用 mcp_call_tool 前必须先用此工具了解参数格式。",
    group="mcp",
    wiki="批量获取指定 MCP 服务上一个或多个工具的完整参数定义（JSON Schema）。"
    "tool_names 是逗号分隔的工具名列表。"
    "在调用 mcp_call_tool 前务必先查看工具参数定义。",
)
async def mcp_get_tool_detail(service_id: str, tool_names: str) -> str:
    """获取 MCP 工具的参数定义。

    Args:
        service_id: MCP 服务唯一标识。
        tool_names: 逗号分隔的工具名列表（如 "create_task,list_tasks"）。

    Returns:
        JSON 格式的工具参数定义列表。
    """
    names = [n.strip() for n in tool_names.split(",") if n.strip()]
    try:
        gw = _get_gateway()
        tools = await gw.get_tool_detail(service_id, names)
    except (Exception, asyncio.CancelledError) as e:
        return _tool_error(f"MCP 工具详情获取失败: {e}")
    return json.dumps(
        [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in tools
        ],
        ensure_ascii=False,
    )


@tool(
    name="mcp_call_tool",
    description="执行 MCP 服务上的工具。必须先用 mcp_get_tool_detail 了解参数格式后再调用。",
    group="mcp",
    wiki="执行指定 MCP 服务上的工具。"
    "arguments 为 JSON 格式的工具参数。"
    "调用前必须先用 mcp_get_tool_detail 查看参数定义。",
)
async def mcp_call_tool(service_id: str, tool_name: str, arguments: str) -> str:
    """执行 MCP 工具。

    Args:
        service_id: MCP 服务唯一标识。
        tool_name: 要执行的工具名称。
        arguments: JSON 格式的工具参数字符串。

    Returns:
        JSON 格式的执行结果。
    """
    try:
        args_dict = json.loads(arguments)
    except json.JSONDecodeError as e:
        return _tool_error(f"参数 JSON 解析失败: {e}")

    try:
        gw = _get_gateway()
        result = await gw.call_tool(service_id, tool_name, args_dict)
    except (Exception, asyncio.CancelledError) as e:
        return _tool_error(f"MCP 工具执行失败: {e}")
    data: dict[str, Any] = {
        "content": result.content,
        "is_error": result.is_error,
    }
    if result.structured_content:
        data["structured_content"] = result.structured_content
    return json.dumps(data, ensure_ascii=False)
