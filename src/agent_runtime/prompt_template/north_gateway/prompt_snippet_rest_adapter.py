"""提示词片段 REST API 接入适配。

提供 FastAPI 路由，支持提示词片段的增删改查。
路由挂载到主应用时使用 prefix="/api/prompt-snippets"。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agent_runtime.prompt_template.north_gateway.prompt_snippet_contracts import (
    PromptSnippetCreateRequest,
    PromptSnippetUpdateRequest,
)
from agent_runtime.prompt_template.north_gateway.prompt_snippet_gateway import (
    PromptSnippetGateway,
)

# ── 请求 / 响应 Pydantic 模型 ───────────────────────────────────


class SnippetSummaryResponse(BaseModel):
    """片段列表项响应体，不包含提示词正文。"""
    name: str
    category: str = "default"


class SnippetResponse(BaseModel):
    """片段详情响应体。"""
    name: str
    text: str
    category: str = "default"


class SnippetCreateBody(BaseModel):
    """创建片段请求体。"""
    name: str
    text: str
    category: str = "default"


class SnippetUpdateBody(BaseModel):
    """更新片段请求体。"""
    new_name: str | None = None
    new_text: str | None = None
    new_category: str | None = None


# ── 路由工厂 ─────────────────────────────────────────────────────


def create_prompt_snippet_router(gateway: PromptSnippetGateway) -> APIRouter:
    """根据网关实例创建提示词片段 REST API 路由。

    Args:
        gateway: 提示词片段北向网关实例。

    Returns:
        FastAPI APIRouter，挂载时建议使用 prefix="/api/prompt-snippets"。
    """
    router = APIRouter()

    @router.get("/", response_model=list[SnippetResponse])
    def list_snippets() -> list[SnippetResponse]:
        """列出所有提示词片段摘要。"""
        snippets = gateway.list_snippets()
        return [
            SnippetResponse(name=s.name, text=s.text, category=s.category)
            for s in snippets
        ]

    @router.get("/{name}", response_model=SnippetResponse)
    def get_snippet(name: str) -> SnippetResponse:
        """根据唯一描述名获取提示词片段。"""
        try:
            snippet = gateway.get_snippet(name)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"提示词片段不存在: {name}")
        return SnippetResponse(name=snippet.name, text=snippet.text, category=snippet.category)

    @router.post("/", response_model=SnippetResponse, status_code=201)
    def create_snippet(body: SnippetCreateBody) -> SnippetResponse:
        """创建提示词片段。"""
        try:
            snippet = gateway.create_snippet(
                PromptSnippetCreateRequest(
                    name=body.name, text=body.text, category=body.category,
                ),
            )
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return SnippetResponse(name=snippet.name, text=snippet.text, category=snippet.category)

    @router.put("/{current_name}", response_model=SnippetResponse)
    def update_snippet(current_name: str, body: SnippetUpdateBody) -> SnippetResponse:
        """更新提示词片段。"""
        if body.new_name is None and body.new_text is None and body.new_category is None:
            raise HTTPException(
                status_code=400, detail="至少指定 new_name、new_text 或 new_category 之一。",
            )
        try:
            snippet = gateway.update_snippet(
                PromptSnippetUpdateRequest(
                    current_name=current_name,
                    new_name=body.new_name,
                    new_text=body.new_text,
                    new_category=body.new_category,
                ),
            )
        except KeyError:
            raise HTTPException(
                status_code=404, detail=f"提示词片段不存在: {current_name}",
            )
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return SnippetResponse(name=snippet.name, text=snippet.text, category=snippet.category)

    @router.delete("/{name}", status_code=204)
    def delete_snippet(name: str) -> None:
        """删除提示词片段。"""
        gateway.delete_snippet(name)

    return router
