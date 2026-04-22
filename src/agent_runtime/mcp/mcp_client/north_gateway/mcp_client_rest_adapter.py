# -*- coding: utf-8 -*-
"""MCP 客户端 REST API 接入适配。

拆分为两组路由：
- /api/mcp-config：MCP 服务配置管理（管理后台，纯本地 CRUD）
- /api/mcp：MCP 服务使用（C 端消费，需连接远程服务）
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agent_runtime.mcp.mcp_client.north_gateway.mcp_client_contracts import (
    McpClientGateway,
    McpServiceAddRequest,
    McpServiceUpdateRequest,
)


# ══════════════════════════════════════════
# Pydantic 请求 / 响应模型
# ══════════════════════════════════════════


class McpServiceSummaryResponse(BaseModel):
    """MCP 服务摘要。"""

    service_id: str
    name: str
    description: str | None = None
    transport_type: str
    enabled: bool
    connected: bool


class McpServiceInfoResponse(BaseModel):
    """MCP 服务配置详情。

    transport 是嵌套字典，如：
        {"type": "stdio", "command": ..., "args": [...], "env": {...}}
        {"type": "streamable-http", "url": ..., "headers": {...}}
    """

    service_id: str
    name: str
    description: str | None = None
    transport: dict[str, Any]
    enabled: bool
    connected: bool
    usage_guide: str | None = None


class McpServiceAddBody(BaseModel):
    """添加 MCP 服务请求体。

    transport 是嵌套字典，结构同 McpServiceInfoResponse.transport。
    """

    service_id: str
    name: str
    transport: dict[str, Any]
    description: str | None = None
    enabled: bool = True
    usage_guide: str | None = None


class McpServiceUpdateBody(BaseModel):
    """更新 MCP 服务请求体（部分更新）。

    transport 若提供，为完整的嵌套字典（整体替换）。
    """

    name: str | None = None
    description: str | None = None
    transport: dict[str, Any] | None = None
    usage_guide: str | None = None


class McpTestResultResponse(BaseModel):
    """连接测试结果。"""

    success: bool
    server_name: str = ""
    server_version: str = ""
    supports_tools: bool = False
    supports_resources: bool = False
    error: str | None = None


class McpServiceDetailResponse(BaseModel):
    """服务能力详情。"""

    supports_tools: bool
    supports_resources: bool
    server_name: str
    server_version: str
    instructions: str | None = None


class McpToolSummaryResponse(BaseModel):
    """工具摘要。"""

    name: str
    description: str | None = None


class McpToolInfoResponse(BaseModel):
    """工具详情（含 inputSchema）。"""

    name: str
    description: str | None = None
    input_schema: dict[str, Any] = {}


class McpToolResultResponse(BaseModel):
    """工具执行结果。"""

    content: str
    structured_content: dict | None = None
    is_error: bool = False


class McpResourceInfoResponse(BaseModel):
    """资源信息。"""

    uri: str
    name: str
    description: str | None = None
    mime_type: str | None = None


class McpResourceContentResponse(BaseModel):
    """资源内容。"""

    uri: str
    mime_type: str | None = None
    text: str | None = None
    blob: str | None = None


class McpToolCallBody(BaseModel):
    """工具调用请求体。"""

    tool_name: str
    arguments: dict[str, Any] = {}


class McpToolDetailBody(BaseModel):
    """批量获取工具详情请求体。"""

    tool_names: list[str]


class StatusResponse(BaseModel):
    """操作状态响应。"""

    status: str
    message: str


# ══════════════════════════════════════════
# 配置管理路由：/api/mcp-config
# ══════════════════════════════════════════


def create_mcp_config_router(gateway: McpClientGateway) -> APIRouter:
    """创建 MCP 服务配置管理 REST API 路由。

    Args:
        gateway: MCP 客户端北向网关实例。

    Returns:
        APIRouter，挂载到 /api/mcp-config 下。
    """
    router = APIRouter()

    @router.get("/", response_model=list[McpServiceSummaryResponse])
    def list_services() -> list[dict]:
        """列出所有已配置的 MCP 服务。"""
        services = gateway.list_services()
        return [
            {
                "service_id": s.service_id,
                "name": s.name,
                "description": s.description,
                "transport_type": s.transport_type,
                "enabled": s.enabled,
                "connected": s.connected,
            }
            for s in services
        ]

    @router.get("/{service_id}", response_model=McpServiceInfoResponse)
    def get_service(service_id: str) -> dict:
        """获取指定 MCP 服务的定义配置。"""
        try:
            info = gateway.get_service(service_id)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return {
            "service_id": info.service_id,
            "name": info.name,
            "description": info.description,
            "transport": info.transport,
            "enabled": info.enabled,
            "connected": info.connected,
            "usage_guide": info.usage_guide,
        }

    @router.post("/", response_model=StatusResponse, status_code=201)
    def add_service(body: McpServiceAddBody) -> dict:
        """添加 MCP 服务配置。"""
        try:
            config = McpServiceAddRequest(
                service_id=body.service_id,
                name=body.name,
                transport=body.transport,
                description=body.description,
                enabled=body.enabled,
                usage_guide=body.usage_guide,
            )
            gateway.add_service(config)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"status": "success", "message": f"已添加 MCP 服务: {body.service_id}"}

    @router.patch("/{service_id}", response_model=StatusResponse)
    def update_service(service_id: str, body: McpServiceUpdateBody) -> dict:
        """更新 MCP 服务配置（部分更新）。"""
        try:
            update = McpServiceUpdateRequest(
                name=body.name,
                description=body.description,
                transport=body.transport,
                usage_guide=body.usage_guide,
            )
            gateway.update_service(service_id, update)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"status": "success", "message": f"已更新 MCP 服务: {service_id}"}

    @router.delete("/{service_id}", response_model=StatusResponse)
    async def remove_service(service_id: str) -> dict:
        """删除 MCP 服务配置。"""
        try:
            await gateway.remove_service(service_id)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return {"status": "success", "message": f"已删除 MCP 服务: {service_id}"}

    @router.post("/{service_id}/enable", response_model=StatusResponse)
    def enable_service(service_id: str) -> dict:
        """启用 MCP 服务。"""
        try:
            gateway.enable_service(service_id)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return {"status": "success", "message": f"已启用 MCP 服务: {service_id}"}

    @router.post("/{service_id}/disable", response_model=StatusResponse)
    def disable_service(service_id: str) -> dict:
        """禁用 MCP 服务。"""
        try:
            gateway.disable_service(service_id)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return {"status": "success", "message": f"已禁用 MCP 服务: {service_id}"}

    return router


# ══════════════════════════════════════════
# MCP 服务使用路由：/api/mcp
# ══════════════════════════════════════════


def create_mcp_router(gateway: McpClientGateway) -> APIRouter:
    """创建 MCP 服务使用 REST API 路由。

    Args:
        gateway: MCP 客户端北向网关实例。

    Returns:
        APIRouter，挂载到 /api/mcp 下。
    """
    router = APIRouter()

    @router.post("/{service_id}/test", response_model=McpTestResultResponse)
    async def test_connection(service_id: str) -> dict:
        """测试 MCP 服务连接。"""
        try:
            result = await gateway.test_connection(service_id)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        data: dict[str, Any] = {"success": result.success}
        if result.success:
            data["server_name"] = result.server_name
            data["server_version"] = result.server_version
            data["supports_tools"] = result.supports_tools
            data["supports_resources"] = result.supports_resources
        else:
            data["error"] = result.error
        return data

    @router.get("/{service_id}/inspect", response_model=McpServiceDetailResponse)
    async def inspect_service(service_id: str) -> dict:
        """查看 MCP 服务的能力和使用指南。"""
        try:
            detail = await gateway.get_service_detail(service_id)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except (ConnectionError, ValueError) as e:
            raise HTTPException(status_code=502, detail=str(e))
        return {
            "supports_tools": detail.supports_tools,
            "supports_resources": detail.supports_resources,
            "server_name": detail.server_name,
            "server_version": detail.server_version,
            "instructions": detail.instructions,
        }

    @router.get(
        "/{service_id}/tools", response_model=list[McpToolSummaryResponse]
    )
    async def list_tools(service_id: str) -> list[dict]:
        """列出 MCP 服务的工具。"""
        try:
            tools = await gateway.list_tools(service_id)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except (ConnectionError, ValueError) as e:
            raise HTTPException(status_code=502, detail=str(e))
        return [{"name": t.name, "description": t.description} for t in tools]

    @router.post(
        "/{service_id}/tool-detail", response_model=list[McpToolInfoResponse]
    )
    async def get_tool_detail(service_id: str, body: McpToolDetailBody) -> list[dict]:
        """批量获取工具参数定义。"""
        try:
            tools = await gateway.get_tool_detail(service_id, body.tool_names)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except (ConnectionError, ValueError) as e:
            raise HTTPException(status_code=502, detail=str(e))
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in tools
        ]

    @router.post(
        "/{service_id}/call-tool", response_model=McpToolResultResponse
    )
    async def call_tool(service_id: str, body: McpToolCallBody) -> dict:
        """执行 MCP 工具。"""
        try:
            result = await gateway.call_tool(
                service_id, body.tool_name, body.arguments
            )
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except (ConnectionError, ValueError) as e:
            raise HTTPException(status_code=502, detail=str(e))
        data: dict[str, Any] = {
            "content": result.content,
            "is_error": result.is_error,
        }
        if result.structured_content:
            data["structured_content"] = result.structured_content
        return data

    @router.get(
        "/{service_id}/resources", response_model=list[McpResourceInfoResponse]
    )
    async def list_resources(service_id: str) -> list[dict]:
        """列出 MCP 服务的资源。"""
        try:
            resources = await gateway.list_resources(service_id)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except (ConnectionError, ValueError) as e:
            raise HTTPException(status_code=502, detail=str(e))
        return [
            {
                "uri": r.uri,
                "name": r.name,
                "description": r.description,
                "mime_type": r.mime_type,
            }
            for r in resources
        ]

    @router.get("/{service_id}/resources/{uri:path}", response_model=McpResourceContentResponse)
    async def read_resource(service_id: str, uri: str) -> dict:
        """读取 MCP 服务的资源内容。"""
        try:
            content = await gateway.read_resource(service_id, uri)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except (ConnectionError, ValueError) as e:
            raise HTTPException(status_code=502, detail=str(e))
        data: dict[str, Any] = {
            "uri": content.uri,
            "mime_type": content.mime_type,
        }
        if content.text is not None:
            data["text"] = content.text
        if content.blob is not None:
            data["blob"] = content.blob
        return data

    return router
