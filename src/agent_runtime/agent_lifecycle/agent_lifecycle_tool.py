"""Agent 生命周期控制内置工具。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_runtime.tool import tool
from agent_runtime.tool.tool_execution_models import (
    BaseToolRequest,
    BaseToolResult,
    FrontendMessage,
    LoopControl,
    LoopDecision,
    TextFormat,
)


AgentLifecycleAction = Literal["notify", "ask_user", "pause", "complete", "terminate", "fail"]


class AgentLifecycleOption(BaseModel):
    """生命周期 ask_user 动作的结构化选项。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="稳定选项标识，供前端回传用户选择。")
    label: str = Field(description="展示给用户的选项文本。")
    description: str | None = Field(default=None, description="可选的选项补充说明。")


class AgentLifecycleRequest(BaseToolRequest):
    """Agent 生命周期工具请求。"""

    action: AgentLifecycleAction = Field(description="生命周期动作。")
    message: str = Field(description="需要展示给用户或记录到生命周期事件中的文本内容。")
    text_format: TextFormat = Field(default="markdown", description="message 的文本格式声明。")
    options: list[AgentLifecycleOption] = Field(default_factory=list, description="ask_user 动作使用的结构化选项。")
    channel: str | None = Field(default=None, description="目标前端通道，例如 web、cli。")
    metadata: dict[str, Any] = Field(default_factory=dict, description="生命周期事件附加元数据。")


@tool(
    name="agent_lifecycle",
    description=(
        "Agent 生命周期与用户沟通工具。用于 notify 同步信息、ask_user 向用户提问或请求确认、"
        "pause 暂停任务、complete 汇报完成、terminate 终止任务、fail 标记失败。"
    ),
    group="agent_lifecycle",
    category="lifecycle",
    always_enabled=True,
    allowed_loop_decisions=("continue", "pause", "complete", "terminate", "fail"),
)
def agent_lifecycle(request: AgentLifecycleRequest) -> BaseToolResult:
    """执行 Agent 生命周期动作并返回统一工具结果。"""
    loop_decision = _loop_decision_for_action(request.action)
    output = {
        "action": request.action,
        "message": request.message,
        "text_format": request.text_format,
        "options": [option.model_dump(mode="json") for option in request.options],
        "channel": request.channel,
        "metadata": request.metadata,
    }
    return BaseToolResult(
        success=True,
        execution_status="success",
        output=output,
        loop_control=LoopControl(
            decision=loop_decision,
            reason=f"agent_lifecycle action={request.action}",
        ),
        frontend_message=FrontendMessage(
            text=request.message,
            text_format=request.text_format,
            channel=request.channel,
            metadata={
                "action": request.action,
                "options": output["options"],
                **request.metadata,
            },
        ),
        metadata={"lifecycle_action": request.action},
    )


def _loop_decision_for_action(action: AgentLifecycleAction) -> LoopDecision:
    """根据生命周期 action 映射 Runtime 循环控制决策。"""
    if action == "notify":
        return "continue"
    if action == "ask_user":
        return "pause"
    if action == "pause":
        return "pause"
    if action == "complete":
        return "complete"
    if action == "terminate":
        return "terminate"
    if action == "fail":
        return "fail"
    return "continue"
