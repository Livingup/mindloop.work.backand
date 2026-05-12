from __future__ import annotations

from pydantic import Field

from agent_runtime.frontend_debug.frontend_debug_factory import FrontendDebugFactory
from agent_runtime.frontend_debug.north_gateway.frontend_debug_contracts import (
    CreateFrontendDebugSessionRequest,
)
from agent_runtime.tool import BaseToolRequest, BaseToolResult, FrontendMessage, tool
from agent_runtime.tool.tool_execution_models import ToolRuntimeContext


class FrontendDebugOpenToolRequest(BaseToolRequest):
    """打开前端调试代理工具请求。"""

    target_url: str = Field(
        description="要调试的前端页面 URL，例如 http://localhost:5173/agents/prompts。仅要求是合法 http/https URL。",
    )
    session_name: str | None = Field(
        default=None,
        description="可选调试会话名称，用于帮助用户区分多个前端调试页面。",
    )


@tool(
    name="frontend_debug_open",
    description=(
        "为指定前端页面创建本机调试代理 URL，并向页面注入元素选择工具条。"
        "用户打开 debug_url 后可点击“选择元素”高亮并选中 DOM 元素，再点击“发送”把元素信息回填到当前聊天输入框。"
    ),
    group="frontend_debug",
    category="business",
)
def frontend_debug_open(request: FrontendDebugOpenToolRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    """创建前端调试代理会话。"""
    gateway = FrontendDebugFactory.create()
    info = gateway.create_session(
        CreateFrontendDebugSessionRequest(
            target_url=request.target_url,
            session_name=request.session_name,
            task_id=ctx.task_id,
            agent_instance_id=ctx.agent_instance_id,
            communication_stream_id=ctx.communication_stream_id,
            request_id=ctx.request_id,
            trace_id=ctx.trace_id,
        )
    )
    output = {
        "session_id": info.session_id,
        "session_name": info.session_name,
        "target_url": info.target_url,
        "proxy_base_url": info.proxy_base_url,
        "debug_url": info.debug_url,
        "status": info.status,
        "communication_stream_id": info.communication_stream_id,
        "usage": "请用户在浏览器打开 debug_url，点击页面右上角的“选择元素”按钮进行 DOM 元素选择。",
    }
    return BaseToolResult(
        success=True,
        execution_status="success",
        output=output,
        frontend_message=FrontendMessage(
            text=(
                "已创建前端调试代理页面。\n\n"
                f"- 目标页面：{info.target_url}\n"
                f"- 调试页面：{info.debug_url}\n\n"
                "打开调试页面后，点击右上角“选择元素”，选中页面元素后点击“发送”，"
                "元素信息会自动填入当前聊天输入框。"
            ),
            text_format="markdown",
            channel="web",
            metadata={
                "frontend_debug_session_id": info.session_id,
                "debug_url": info.debug_url,
                "target_url": info.target_url,
            },
        ),
        metadata={"frontend_debug_session_id": info.session_id},
    )
