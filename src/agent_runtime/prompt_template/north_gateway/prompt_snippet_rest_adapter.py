"""提示词片段 REST API 接入适配。

提供 FastAPI 路由，支持提示词片段的增删改查。
路由挂载到主应用时使用 prefix="/api/prompt-snippets"。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agent_runtime.prompt_template.north_gateway.prompt_snippet_contracts import (
    PromptSnippetCreateRequest,
    PromptSnippetEditRequest,
    PromptSnippetTextEdit,
    PromptSnippetUpdateRequest,
)
from agent_runtime.prompt_template.north_gateway.prompt_snippet_gateway import (
    PromptSnippetGateway,
)
from agent_runtime.prompt_template.prompt_snippet import (
    PromptSnippetConflictError,
    PromptSnippetValidationError,
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


class SnippetContentBody(BaseModel):
    """提示词片段正文更新请求体。"""
    text: str


class PromptSnippetEditItem(BaseModel):
    """单项连续行编辑。"""
    search_lines: list[str]
    replace_lines: list[str] = []


class PromptSnippetEditContentBody(BaseModel):
    """提示词正文局部编辑请求体。"""
    edits: list[PromptSnippetEditItem]
    expected_text_sha256: str | None = None


# ── 路由工厂 ─────────────────────────────────────────────────────


def create_prompt_snippet_router(gateway: PromptSnippetGateway) -> APIRouter:
    """根据网关实例创建提示词片段 REST API 路由。

    Args:
        gateway: 提示词片段北向网关实例。

    Returns:
        FastAPI APIRouter，挂载时建议使用 prefix="/api/prompt-snippets"。
    """
    router = APIRouter()

    @router.get("/", response_model=list[dict])
    def list_snippets(summary: bool = False) -> list[dict]:
        """列出所有提示词片段；summary=true 时不返回正文。"""
        snippets = gateway.list_snippets()
        if summary:
            return [
                SnippetSummaryResponse(name=s.name, category=s.category).model_dump()
                for s in snippets
            ]
        return [
            SnippetResponse(name=s.name, text=s.text, category=s.category).model_dump()
            for s in snippets
        ]

    @router.get("/builtin-variables", response_model=list[dict[str, str]])
    def get_builtin_variables() -> list[dict[str, str]]:
        """获取系统内置提示词变量定义列表（包含占位符、含义说明与取值逻辑说明）。"""
        from agent_runtime.prompt_template.prompt_variables import list_builtin_prompt_variables

        return list_builtin_prompt_variables()

    @router.get("/{name}", response_model=SnippetResponse)
    def get_snippet(name: str) -> SnippetResponse:
        """根据唯一描述名获取提示词片段。"""
        try:
            snippet = gateway.get_snippet(name)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"提示词片段不存在: {name}")
        return SnippetResponse(name=snippet.name, text=snippet.text, category=snippet.category)

    @router.get("/{name}/content", response_model=dict[str, str])
    def get_snippet_content(name: str) -> dict[str, str]:
        """获取提示词片段正文，供管理脚本按 ID 拉取长文本。"""
        try:
            snippet = gateway.get_snippet(name)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"提示词片段不存在: {name}")
        return {"name": snippet.name, "text": snippet.text}

    @router.put("/{name}/content", response_model=SnippetResponse)
    def set_snippet_content(name: str, body: SnippetContentBody) -> SnippetResponse:
        """仅替换提示词片段正文，不修改名称和分类。"""
        try:
            snippet = gateway.update_snippet(
                PromptSnippetUpdateRequest(current_name=name, new_text=body.text),
            )
        except KeyError:
            raise HTTPException(status_code=404, detail=f"提示词片段不存在: {name}")
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return SnippetResponse(name=snippet.name, text=snippet.text, category=snippet.category)

    @router.patch("/{name}/content", response_model=SnippetResponse)
    def patch_snippet_content(name: str, body: SnippetContentBody) -> SnippetResponse:
        """仅替换提示词片段正文；PATCH 作为内容子资源的部分写入口。"""
        return set_snippet_content(name, body)

    @router.post("/{name}/content/edits", response_model=SnippetResponse)
    def edit_snippet_content(name: str, body: PromptSnippetEditContentBody) -> SnippetResponse:
        """按连续行唯一匹配局部编辑提示词正文。"""
        if not body.edits:
            raise HTTPException(status_code=400, detail="edits 列表不能为空。")
        for edit in body.edits:
            if not edit.search_lines:
                raise HTTPException(status_code=400, detail="search_lines 必须至少包含一行。")

        try:
            request = PromptSnippetEditRequest(
                name=name,
                edits=[
                    PromptSnippetTextEdit(
                        search_lines=item.search_lines,
                        replace_lines=item.replace_lines,
                    )
                    for item in body.edits
                ],
                expected_text_sha256=body.expected_text_sha256,
            )
            snippet = gateway.edit_snippet_content(request)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"提示词片段不存在: {name}")
        except PromptSnippetValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except (PromptSnippetConflictError, ValueError) as e:
            raise HTTPException(status_code=409, detail=str(e))
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

    @router.patch("/{current_name}", response_model=SnippetResponse)
    def patch_snippet(current_name: str, body: SnippetUpdateBody) -> SnippetResponse:
        """部分更新提示词片段元数据或正文，语义与 PUT 保持一致。"""
        return update_snippet(current_name, body)

    @router.delete("/{name}", status_code=204)
    def delete_snippet(name: str) -> None:
        """删除提示词片段。"""
        gateway.delete_snippet(name)

    return router
