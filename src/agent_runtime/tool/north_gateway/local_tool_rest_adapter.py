"""本地工具 REST API 接入适配。

提供 FastAPI 路由，支持工具的列出、查询、执行和 wiki 列表。
路由挂载到主应用时使用 prefix="/api/tools"。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agent_runtime.tool.north_gateway.local_tool_contracts import (
    LocalToolGateway,
    ToolExecuteRequest,
)
from agent_runtime.tool.tool_execution_models import LoopDecision, ToolCategory


# ── 请求 / 响应 Pydantic 模型 ───────────────────────────────────


class ToolSummaryResponse(BaseModel):
    """工具摘要响应体。"""
    tool_name: str
    description: str
    group: str
    wiki: str | None = None
    category: ToolCategory = "business"
    always_enabled: bool = False
    allowed_loop_decisions: tuple[LoopDecision, ...] = ("continue",)


class ToolDetailResponse(BaseModel):
    """工具详情响应体。"""
    tool_name: str
    description: str
    group: str
    public: bool
    wiki: str | None
    category: ToolCategory
    always_enabled: bool
    allowed_loop_decisions: tuple[LoopDecision, ...]
    schemas: dict[str, dict[str, Any]] = {}


class ToolWikiItemResponse(BaseModel):
    """工具 wiki 条目响应体。"""
    tool_name: str
    wiki: str | None
    group: str
    category: ToolCategory = "business"
    always_enabled: bool = False
    allowed_loop_decisions: tuple[LoopDecision, ...] = ("continue",)
    schemas: dict[str, dict[str, Any]] = {}


class ToolExecBody(BaseModel):
    """工具执行请求体。"""
    params: dict[str, Any] = {}
    request_id: str | None = None
    task_id: str | None = None
    agent_instance_id: str | None = None
    trace_id: str | None = None
    communication_stream_id: str | None = None


class ToolExecResponse(BaseModel):
    """工具执行响应体。"""
    tool_name: str
    result: Any
    success: bool
    execution_status: str
    error_code: str | None = None
    error_message: str | None = None
    output: Any = None
    loop_control: dict[str, Any]
    frontend_message: dict[str, Any] | None = None
    metadata: dict[str, Any] = {}


# ── 路由工厂 ─────────────────────────────────────────────────────


def create_local_tool_router(gateway: LocalToolGateway) -> APIRouter:
    """根据网关实例创建本地工具 REST API 路由。

    Args:
        gateway: 本地工具北向网关实例。

    Returns:
        FastAPI APIRouter，挂载时建议使用 prefix="/api/tools"。
    """
    router = APIRouter()

    @router.get("/", response_model=list[ToolSummaryResponse])
    def list_tools(
        group: str | None = None,
        group_by: bool = False,
    ) -> Any:
        """列出所有公开工具。"""
        result = gateway.list_tools(public_only=True, group_by=group_by)
        if group_by:
            output: dict[str, list[dict[str, Any]]] = {}
            for group_name, items in result.items():
                if group and group_name != group:
                    continue
                output[group_name] = [
                    {"tool_name": i.tool_name, "description": i.description,
                     "group": i.group, "wiki": i.wiki,
                     "category": i.category,
                     "always_enabled": i.always_enabled,
                     "allowed_loop_decisions": i.allowed_loop_decisions}
                    for i in items
                ]
            return output
        items = result
        if group:
            items = [i for i in items if i.group == group]
        return [
            ToolSummaryResponse(
                tool_name=i.tool_name, description=i.description,
                group=i.group, wiki=i.wiki,
                category=i.category,
                always_enabled=i.always_enabled,
                allowed_loop_decisions=i.allowed_loop_decisions,
            )
            for i in items
        ]

    @router.get("/wiki", response_model=dict[str, list[ToolWikiItemResponse]])
    def wiki_list() -> Any:
        """获取工具 wiki 列表（按 group 分组）。"""
        result = gateway.get_wiki_list()
        output: dict[str, list[dict[str, Any]]] = {}
        for group_name, items in result.items():
            output[group_name] = [
                {"tool_name": i.tool_name, "wiki": i.wiki,
                 "group": i.group,
                 "category": i.category,
                 "always_enabled": i.always_enabled,
                 "allowed_loop_decisions": i.allowed_loop_decisions,
                 "schemas": i.schemas}
                for i in items
            ]
        return output

    @router.get("/{name}", response_model=ToolDetailResponse)
    def get_tool(name: str) -> ToolDetailResponse:
        """查询指定工具详情。"""
        try:
            detail = gateway.get_tool(name)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"本地工具不存在: {name}")
        return ToolDetailResponse(
            tool_name=detail.tool_name, description=detail.description,
            group=detail.group, public=detail.public,
            wiki=detail.wiki,
            category=detail.category,
            always_enabled=detail.always_enabled,
            allowed_loop_decisions=detail.allowed_loop_decisions,
            schemas=detail.schemas,
        )

    @router.post("/{name}/exec", response_model=ToolExecResponse)
    async def exec_tool(name: str, body: ToolExecBody) -> ToolExecResponse:
        """执行指定工具。"""
        try:
            exec_result = await gateway.execute_tool(
                ToolExecuteRequest(
                    tool_name=name,
                    params=body.params,
                    request_id=body.request_id,
                    task_id=body.task_id,
                    agent_instance_id=body.agent_instance_id,
                    trace_id=body.trace_id,
                    communication_stream_id=body.communication_stream_id,
                ),
            )
        except KeyError:
            raise HTTPException(status_code=404, detail=f"本地工具不存在: {name}")
        result_payload = exec_result.model_dump(mode="json")
        return ToolExecResponse(
            **result_payload,
            result=exec_result.result,
        )

    return router
