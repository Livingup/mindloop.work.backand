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


# ── 请求 / 响应 Pydantic 模型 ───────────────────────────────────


class ToolSummaryResponse(BaseModel):
    """工具摘要响应体。"""
    tool_name: str
    description: str
    group: str
    wiki: str | None = None


class ToolDetailResponse(BaseModel):
    """工具详情响应体。"""
    tool_name: str
    description: str
    group: str
    public: bool
    wiki: str | None
    schemas: dict[str, dict[str, Any]] = {}


class ToolWikiItemResponse(BaseModel):
    """工具 wiki 条目响应体。"""
    tool_name: str
    wiki: str | None
    group: str
    schemas: dict[str, dict[str, Any]] = {}


class ToolExecBody(BaseModel):
    """工具执行请求体。"""
    params: dict[str, Any] = {}


class ToolExecResponse(BaseModel):
    """工具执行响应体。"""
    tool_name: str
    result: Any


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
                     "group": i.group, "wiki": i.wiki}
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
                 "group": i.group, "schemas": i.schemas}
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
            wiki=detail.wiki, schemas=detail.schemas,
        )

    @router.post("/{name}/exec", response_model=ToolExecResponse)
    async def exec_tool(name: str, body: ToolExecBody) -> ToolExecResponse:
        """执行指定工具。"""
        try:
            exec_result = await gateway.execute_tool(
                ToolExecuteRequest(tool_name=name, params=body.params),
            )
        except KeyError:
            raise HTTPException(status_code=404, detail=f"本地工具不存在: {name}")
        except TypeError as e:
            raise HTTPException(status_code=400, detail=f"参数不匹配: {e}")
        return ToolExecResponse(
            tool_name=exec_result.tool_name, result=exec_result.result,
        )

    return router
