"""统一工具执行协议模型。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


ToolExecutionStatus = Literal["success", "failed", "skipped"]
LoopDecision = Literal["continue", "pause", "complete", "fail"]
TextFormat = Literal["plain_text", "markdown", "json", "html"]
ToolCategory = Literal["business", "lifecycle", "system"]


class FrontendMessage(BaseModel):
    """面向用户前端的沟通消息。"""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(description="需要展示给用户的文本内容。")
    text_format: TextFormat = Field(default="markdown", description="文本内容的格式声明；可选值仅限 plain_text、markdown、json、html。")
    channel: str | None = Field(default=None, description="目标前端通道，例如 web、cli。")
    metadata: dict[str, Any] = Field(default_factory=dict, description="前端渲染或业务追踪所需的附加元数据。")


class LoopControl(BaseModel):
    """工具结果对 Agent 执行循环表达的下一步决策。"""

    model_config = ConfigDict(extra="forbid")

    decision: LoopDecision = Field(default="continue", description="Runtime 应采用的下一步循环控制决策；可选值仅限 continue、pause、complete、fail。")
    reason: str | None = Field(default=None, description="产生该循环决策的业务原因。")


class BaseToolRequest(BaseModel):
    """所有工具请求模型的统一基类。

    只承载工具业务参数的语义，不携带 Runtime 调度元数据（如 request_id / task_id /
    agent_instance_id / trace_id）。这些 Runtime 上下文不属于 LLM 可见的工具参数，
    不应出现在工具 JSON Schema 里污染 LLM 上下文，也不应让 LLM 自己填值。

    如果工具需要 Runtime 上下文（如当前 task / agent_instance / 跟踪链路），应通过
    `ToolRuntimeContext` 显式声明在工具函数签名上，由框架自动注入。
    """

    model_config = ConfigDict(extra="allow")


class ToolRuntimeContext(BaseModel):
    """工具 Runtime 上下文，由 Agent 调度链路注入工具实现函数。

    工具函数若需要访问当前调用的 Runtime 上下文（task / agent_instance / trace 等），
    可在签名中声明类型为 ToolRuntimeContext 的参数（推荐命名 `ctx`）：

    ```python
    @tool(name="end_task", ...)
    def end_task(request: EndTaskRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
        logger.info("ending task=%s instance=%s", ctx.task_id, ctx.agent_instance_id)
        ...
    ```

    框架会自动识别签名中的 ToolRuntimeContext 参数，从工具 JSON Schema 中跳过该参数
    （不暴露给 LLM），并在执行时注入实参。不声明该参数的工具完全不受影响。
    """

    model_config = ConfigDict(extra="allow")

    request_id: str | None = Field(default=None, description="本次工具调用的请求标识。")
    task_id: str | None = Field(default=None, description="所属 Agent 任务标识。")
    agent_instance_id: str | None = Field(default=None, description="所属 Agent 运行时实例标识。")
    conversation_id: str | None = Field(default=None, description="所属 Conversation 标识。")
    conversation_history_copy_message_count: int | None = Field(
        default=None,
        description="触发工具调用的当前 LLM 请求开始前，Conversation 中可被子 Agent 继承的历史消息数量。",
    )
    trace_id: str | None = Field(default=None, description="执行链路追踪标识。")
    communication_stream_id: str | None = Field(default=None, description="所属通信流标识。")


class BaseToolResult(BaseModel):
    """所有工具执行结果模型的统一基类。"""

    model_config = ConfigDict(extra="allow")

    success: bool = Field(default=True, description="工具是否执行成功。")
    execution_status: ToolExecutionStatus = Field(default="success", description="工具执行状态；可选值仅限 success、failed、skipped。")
    error_code: str | None = Field(default=None, description="工具执行失败时的机器可读错误码。")
    error_message: str | None = Field(default=None, description="工具执行失败时的人类可读错误信息。")
    output: Any = Field(default=None, description="工具业务输出。")
    loop_control: LoopControl = Field(default_factory=LoopControl, description="工具主动返回的循环控制决策。")
    frontend_message: FrontendMessage | None = Field(default=None, description="需要前端展示的沟通消息。")
    metadata: dict[str, Any] = Field(default_factory=dict, description="调试、审计或业务追踪所需附加信息。")

    @property
    def loop_decision(self) -> LoopDecision:
        """兼容性快捷属性，返回 loop_control.decision。"""
        return self.loop_control.decision

    def to_serializable_dict(self) -> dict[str, Any]:
        """转换为适合写入会话历史和 REST/CLI 输出的字典。"""
        return self.model_dump(mode="json")

    def __eq__(self, other: Any) -> bool:
        """兼容旧调用方直接拿工具原始返回值做相等比较的场景。"""
        if isinstance(other, BaseToolResult):
            return super().__eq__(other)
        return self.output == other


class ToolResultEnvelope(BaseToolResult):
    """带工具名称的统一执行结果信封。"""

    tool_name: str = Field(description="被执行的工具名称。")


def wrap_tool_output(output: Any) -> BaseToolResult:
    """把旧工具返回值包装成统一工具结果。"""
    if isinstance(output, BaseToolResult):
        return output
    return BaseToolResult(output=output)


def build_tool_error_result(
    *,
    error_message: str,
    error_code: str | None = None,
    output: Any = None,
    loop_decision: LoopDecision = "continue",
) -> BaseToolResult:
    """构建统一工具错误结果。"""
    return BaseToolResult(
        success=False,
        execution_status="failed",
        error_code=error_code,
        error_message=error_message,
        output=output,
        loop_control=LoopControl(decision=loop_decision, reason=error_message),
    )
