# -*- coding: utf-8 -*-
"""本地子 Agent 协作工具。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from agent_runtime.tool import tool
from agent_runtime.tool.tool_execution_models import BaseToolRequest, BaseToolResult, ToolRuntimeContext
from agent_runtime.subagent.subagent_runtime import get_subagent_runtime


class SubagentMessageAttachmentRequest(BaseModel):
    """LLM 可见的本地 Agent 协作附件。

    这里只暴露模型能理解和转发的 URL，不暴露 Runtime 内部 resource_id。
    """

    kind: str = Field(description="附件类型，仅支持 image 或 pdf。")
    url: str = Field(description="图片或 PDF 的 http/https 文件 URL。Runtime 会自动反查或接入为内部文件资源。")
    name: str | None = Field(default=None, description="可选文件名，用于接收方 UI 展示。")
    mime_type: str | None = Field(default=None, description="可选 MIME 类型，例如 image/png 或 application/pdf；为空时由 URL 响应或后缀推断。")


class CreateSubagentRequest(BaseToolRequest):
    """创建直接子 Agent。"""

    agent_definition_id: str = Field(description="要创建的子 AgentDefinition ID，必须包含在当前 AgentDefinition.available_sub_agent_ids 中。")
    llm_policy_group_id: str | None = Field(default=None, description="可选 LLM 执行策略组 ID；不填时使用子 Agent 默认策略或继承当前 Agent 策略。")
    initial_message: str | None = Field(default=None, description="可选初始任务消息；提供后会在创建成功后异步投递给子 Agent。")
    initial_message_attachments: list[SubagentMessageAttachmentRequest] = Field(default_factory=list, description="可选初始消息附件 URL 列表，支持图片或 PDF。")


class SendAgentMessageRequest(BaseToolRequest):
    """向直接父/子 Agent 发送消息。"""

    to_agent_instance_id: str = Field(description="目标 Agent 实例 ID，必须是未释放的直接父 Agent 或直接子 Agent。")
    content: str = Field(default="", description="要发送给目标 Agent 的任务协作消息正文。发送立即返回，仅表示已接受/入队。")
    attachments: list[SubagentMessageAttachmentRequest] = Field(default_factory=list, description="可选协作附件 URL 列表，支持图片或 PDF。")


class GetAgentStatusRequest(BaseToolRequest):
    """查询同一任务内直接父/子 Agent 的公开状态。"""

    agent_instance_id: str = Field(description="要查询的 Agent 实例 ID；可为自身、直接父 Agent 或直接子 Agent。")


class EndSubagentRequest(BaseToolRequest):
    """释放当前 Agent 直接创建的一个或多个子 Agent。"""

    child_agent_instance_ids: list[str] = Field(description="要释放的直接子 Agent 实例 ID 列表；只释放一个子 Agent 时传入单元素列表。每个条目都必须是当前 Agent 直接创建且尚未释放的子 Agent。")
    reason: str = Field(default="parent_agent_ended_subagent", description="释放原因摘要。")



@tool(
    name="create_subagent",
    description=(
        "创建一个本地子 Agent 实例。只能创建当前 AgentDefinition.available_sub_agent_ids 允许的直接子 Agent。"
        "创建成功后立即发布 agent.subagent.created 场景事件，前端可据此立刻增加 Agent 标签页。"
    ),
    group="subagent",
    category="business",
    allowed_loop_decisions=("continue",),
)
async def create_subagent(request: CreateSubagentRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    if not ctx.agent_instance_id:
        raise ValueError("缺少当前 Agent 实例上下文，不能创建子 Agent。")
    output = await get_subagent_runtime().create_subagent(
        parent_instance_id=ctx.agent_instance_id,
        agent_definition_id=request.agent_definition_id,
        llm_policy_group_id=request.llm_policy_group_id,
        initial_message=request.initial_message,
        initial_message_attachments=[item.model_dump() for item in request.initial_message_attachments],
    )
    return BaseToolResult(output=output, metadata={"subagent_action": "create_subagent"})


@tool(
    name="send_agent_message",
    description=(
        "向当前任务内未释放的直接父 Agent 或直接子 Agent 发送本地协作消息。"
        "发送是异步立即返回语义：工具结果只表示目标 Agent 已接受或入队，等待回复请另行调用 pause_task。"
    ),
    group="subagent",
    category="business",
    allowed_loop_decisions=("continue",),
)
async def send_agent_message(request: SendAgentMessageRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    if not ctx.agent_instance_id:
        raise ValueError("缺少当前 Agent 实例上下文，不能发送 Agent 消息。")
    output = await get_subagent_runtime().send_agent_message(
        from_instance_id=ctx.agent_instance_id,
        to_instance_id=request.to_agent_instance_id,
        content=request.content,
        attachments=[item.model_dump() for item in request.attachments],
    )
    return BaseToolResult(output=output, metadata={"subagent_action": "send_agent_message"})


@tool(
    name="get_agent_status",
    description="查询当前 Agent 自身、未释放直接父 Agent 或未释放直接子 Agent 的公开状态：running / paused / ended。",
    group="subagent",
    category="business",
    allowed_loop_decisions=("continue",),
)
def get_agent_status(request: GetAgentStatusRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    if not ctx.agent_instance_id:
        raise ValueError("缺少当前 Agent 实例上下文，不能查询 Agent 状态。")
    output = get_subagent_runtime().get_agent_status(
        requester_instance_id=ctx.agent_instance_id,
        target_instance_id=request.agent_instance_id,
    )
    return BaseToolResult(output=output, metadata={"subagent_action": "get_agent_status"})


@tool(
    name="end_subagent",
    description="释放当前 Agent 直接创建的一个或多个子 Agent，并级联释放每个子 Agent 的未释放后代。",
    group="subagent",
    category="business",
    allowed_loop_decisions=("continue",),
)
async def end_subagent(request: EndSubagentRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    if not ctx.agent_instance_id:
        raise ValueError("缺少当前 Agent 实例上下文，不能释放子 Agent。")
    output = await get_subagent_runtime().end_subagents(
        parent_instance_id=ctx.agent_instance_id,
        child_instance_ids=request.child_agent_instance_ids,
        reason=request.reason,
    )
    return BaseToolResult(output=output, metadata={"subagent_action": "end_subagent"})


@tool(
    name="list_subagents",
    description="列出当前 Agent 直接创建且尚未释放的子 Agent。只返回直接子 Agent，不返回孙级或历史已释放子 Agent。",
    group="subagent",
    category="business",
    allowed_loop_decisions=("continue",),
)
def list_subagents(ctx: ToolRuntimeContext) -> BaseToolResult:
    if not ctx.agent_instance_id:
        raise ValueError("缺少当前 Agent 实例上下文，不能列出子 Agent。")
    output = get_subagent_runtime().list_subagents(ctx.agent_instance_id)
    return BaseToolResult(output={"subagents": output}, metadata={"subagent_action": "list_subagents"})
