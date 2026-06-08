"""Agent 生命周期控制内置工具。

拆分为 4 个独立工具：
1. message_user — 向用户发送消息（notify 不阻塞同步 / request_response 阻塞等用户回复）
2. pause_task — 暂停任务，被动等待外部事件唤醒
3. end_task — 结束任务并退出（用于任务成功完成）
4. fail_task — 报告 Agent 不可恢复的失败并终止任务
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_runtime.tool import tool
from agent_runtime.tool.tool_execution_models import (
    BaseToolRequest,
    BaseToolResult,
    FrontendMessage,
    LoopControl,
)


# ── 公共模型 ─────────────────────────────────────────────────────────

MessageAction = Literal["notify", "request_response"]
MessageUserTextFormat = Literal["html"]


class MessageOption(BaseModel):
    """request_response 动作的结构化选项。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="稳定选项标识，供前端回传用户选择。")
    label: str = Field(description="展示给用户的选项文本。")
    description: str | None = Field(default=None, description="可选的选项补充说明。")


# ── Tool 1: message_user ─────────────────────────────────────────────

class MessageUserRequest(BaseToolRequest):
    """向用户发送消息工具请求。"""

    action: MessageAction = Field(
        description=(
            "选择消息发送模式（决定是否阻塞 Agent 循环）；可选值仅限 notify、request_response。\n"
            "\n"
            "• notify：单向同步消息，发完后不阻塞，Agent 继续推进任务。"
            "适用：阶段性进度报告、即将执行耗时操作的预告、发现重要事实但不需用户决策、关键状态广播。"
            "禁止：不要用 notify 提问或表达“等用户回复”、“请告诉我你想要什么”，这种场景必须用 request_response。\n"
            "\n"
            "• request_response：发送消息并阻塞等待用户回复后才能继续。"
            "覆盖所有需要用户回应的场景：提问、请求确认、请求选择、汇报成果等待审核、危险操作前的人工确认、向用户求助等。"
            "要求：message 必须明确表达你需要用户做什么；如果有明确候选项应填入 options 字段供前端展示选择按钮。"
            "与 end_task 配合：任务做完后先用 request_response 汇报成果等待用户验收，得到肯定反馈后再调 end_task 终止任务。"
        )
    )
    message: str = Field(
        description=(
            "发送给用户的 HTML fragment 正文。"
            "notify 时交代要同步的进度/发现/状态；"
            "request_response 时清晰说明你需要用户做什么（提问、请求审核、请求确认、求助等）。"
            "内容必须符合 text_format=html 的 HTML fragment 技术规范，不要写 Markdown。"
            "结构化内容优先使用表格表达对比、范围、状态、计划或结果；"
            "段落列表建议按“无符号标题/段落 → 圆点主项 → 短横线细节”表达，最多不超过三级，不必强行凑满三级。"
        ),
    )
    text_format: MessageUserTextFormat = Field(
        default="html",
        description=(
            "message 的文本格式；唯一允许值是 html。"
            "当 text_format=html 时，message 必须是可直接插入页面正文区域的 HTML fragment："
            "不要输出完整文档结构（禁止 <!doctype>、<html>、<head>、<body>），不要输出 Markdown 语法。"
            "优先使用语义化 HTML 标签表达结构，例如 article、header、section、footer、h1、h2、h3、p、ul、ol、li、dl、dt、dd、blockquote、pre、code、table、thead、tbody、tr、th、td、strong、em、a、small、sup、br、hr；支持静态内嵌 SVG（svg、path、circle、rect、line、polyline、polygon、text 等展示标签）和 MathML（math、mrow、mi、mn、mo、msup、msub、mfrac、msqrt 等数学公式标签）。"
            "不要输出展示绑定或脚本相关内容：禁止 class、style、id、data-*、on* 事件属性，禁止 script、style、iframe、object、embed、canvas、form、input、button。"
            "静态 SVG/MathML 仅用于展示：禁止 SVG script、foreignObject、animate、set 等脚本、动画或交互标签，禁止外部资源引用或可执行内容；SVG 可使用 viewBox、width、height、fill、stroke 等几何和展示属性。"
            "链接使用 <a href=\"https://...\">文本</a>，外部 URL 不要裸写；代码块使用 <pre><code>...</code></pre>；列表使用 ul/ol/li；表格使用 table 语义标签。"
            "为提升可读性，多维信息优先使用 table；段落列表最多三层：第 1 层用无符号标题/段落，第 2 层用圆点主项，第 3 层用短横线细节；少于三层时保持简单，不要强行嵌套。"
            "插入来自用户、工具、网页或外部系统的动态文本时，必须进行 HTML 字符转义：& 转为 &amp;，< 转为 &lt;，> 转为 &gt;，\" 转为 &quot;，避免把外部文本当作 HTML 执行。"
        ),
    )
    options: list[MessageOption] = Field(
        default_factory=list,
        description=(
            "仅 request_response 使用的结构化选项列表，供前端展示为选择按钮。"
            "每个选项包含 id（稳定标识，供前端回传不依赖 label）、label（展示文本）、可选 description（补充说明）。"
            "notify 留空。"
        ),
    )
    channel: str | None = Field(
        default=None,
        description="目标前端通道标签（如 web/cli）。一般不需要填，默认沿用当前会话的通信流；仅当需要明确路由到特定前端时才设置。",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="附加业务元数据（如事件标签、业务跟踪 id）。一般留空。",
    )


@tool(
    name="message_user",
    description=(
        "向用户发送消息。这是 Agent 与用户沟通的【唯一】渠道——所有面向用户的展示性输出都必须走此工具，不要用普通 assistant 文本输出给用户。\n"
        "\n"
        "【做什么】将 message 作为 HTML fragment 发送到前端展示。通过 action 选择行为模式：\n"
        "  • notify：发完即继续，Agent 不阻塞（单向告知）\n"
        "  • request_response：发完后阻塞等用户回复（双向沟通）\n"
        "\n"
        "【何时使用】\n"
        "• 把进度、发现、阶段性成果、关键状态告知用户（用 notify）\n"
        "• 需要用户提供决策、选择、确认、审核才能继续（用 request_response）\n"
        "• 任务完成需要用户验收（用 request_response）\n"
        "• 遇到无法判断的场景需要向用户求助（用 request_response）\n"
        "\n"
        "【HTML 输出协议】\n"
        "• text_format 必须填 html，message 必须是 HTML fragment，不是 Markdown，也不是完整 HTML 文档。\n"
        "• 优先使用语义化内容标签表达结构，可使用静态内嵌 SVG 和 MathML；不要写 class/style/id/data-* 或任何 on* 事件属性。\n"
        "• 禁止 script/style/iframe/object/embed/canvas/form/input/button 等可执行或交互标签；静态 SVG/MathML 仅用于展示，禁止 SVG script、foreignObject、animate、set 等脚本、动画或交互标签，禁止外部资源引用或可执行内容。\n"
        "• 外部文本必须 HTML 转义；链接使用 a 标签，代码使用 pre/code，列表使用 ul/ol/li，表格使用 table 语义标签。\n"
        "• 为了让前端展示更美观整洁，有对比、范围、状态、计划或结果类信息时尽量用 table；段落列表采用最多三级的视觉层级：无符号标题/段落 → 圆点主项 → 短横线细节，少于三级即可表达清楚时不要强行凑满。\n"
        "\n"
        "【不要用于】\n"
        "• Agent 内部推理/规划/反思（这些是思考过程，用户不需要看）\n"
        "• 等待其他 Agent 消息或系统事件 → 改用 pause_task\n"
        "• 终止任务 → 成功用 end_task、失败用 fail_task。message_user 只负责发消息不负责生命周期。"
    ),
    group="agent_lifecycle",
    category="lifecycle",
    always_enabled=True,
    allowed_loop_decisions=("continue", "pause"),
)
def message_user(request: MessageUserRequest) -> BaseToolResult:
    """向用户发送消息并返回统一工具结果。"""
    decision_map = {
        "notify": "continue",
        "request_response": "pause",
    }
    loop_decision = decision_map[request.action]

    output_map = {
        "notify": "Message delivered to user.",
        "request_response": "Waiting for user response.",
    }

    return BaseToolResult(
        success=True,
        execution_status="success",
        output=output_map[request.action],
        loop_control=LoopControl(
            decision=loop_decision,
            reason=f"message_user action={request.action}",
        ),
        frontend_message=FrontendMessage(
            text=request.message,
            text_format=request.text_format,
            channel=request.channel,
            metadata={
                "action": request.action,
                "options": [opt.model_dump(mode="json") for opt in request.options],
                **request.metadata,
            },
        ),
        metadata={"lifecycle_action": request.action},
    )


# ── Tool 2: pause_task ───────────────────────────────────────────────

class PauseTaskRequest(BaseToolRequest):
    """暂停任务工具请求。"""

    reason: str = Field(
        description=(
            "暂停的明确原因，说明在等待什么事件或消息。"
            "会被记录到 trace 供后续追查，不会发送给用户。"
        ),
    )


@tool(
    name="pause_task",
    description=(
        "暂停 Agent 循环，被动等待【外部系统事件】或【其他 Agent 的消息】唤醒。Agent 自身不再做任何动作，循环挂起。不向用户发任何消息。\n"
        "\n"
        "【做什么】触发 loop_decision=pause 让 Runtime 暂停当前 Agent 实例。reason 字段记录在等什么事件，供运维追查。\n"
        "\n"
        "【何时使用】\n"
        "• 多 Agent 协作：等另一个 Agent 把结果推送过来后再继续\n"
        "• 系统事件等待：等定时器触发、外部 webhook 回调、状态变更通知\n"
        "• 长耗时异步任务：已发起异步任务，等系统通知任务完成\n"
        "\n"
        "【不要用于】\n"
        "• 等用户回复 → 必须用 message_user(action=request_response)。pause_task 不发消息给用户，用户不会看到你在等什么\n"
        "• 不知道下一步怎么做 → 用 message_user(action=request_response) 求助用户\n"
        "• 任务卡住推进不下去 → 用 fail_task 报告失败\n"
        "\n"
        "【关键区分】pause_task 是“被动等”——Agent 知道一定会有外部事件唤醒。如果不确定有没有事件来，不要用 pause_task。"
    ),
    group="agent_lifecycle",
    category="lifecycle",
    always_enabled=True,
    allowed_loop_decisions=("pause",),
)
def pause_task(request: PauseTaskRequest) -> BaseToolResult:
    """暂停任务等待外部事件。"""
    return BaseToolResult(
        success=True,
        execution_status="success",
        output="Task paused, waiting for external event.",
        loop_control=LoopControl(
            decision="pause",
            reason=f"pause_task: {request.reason}",
        ),
        metadata={"lifecycle_action": "pause"},
    )


# ── Tool 3: end_task ─────────────────────────────────────────────────

class EndTaskRequest(BaseToolRequest):
    """结束任务工具请求。"""

    summary: str = Field(
        description=(
            "任务最终总结，简要说明完成情况和关键交付物。"
            "会被记录到任务 trace 中作为任务完成凭证。"
        ),
    )


@tool(
    name="end_task",
    description=(
        "任务【成功完成】时调用，结束当前任务会话并退出 Agent 循环。任务进入终态，不可逆。end_task 仅表达“成功”语义。\n"
        "\n"
        "【做什么】触发 loop_decision=complete 让 Runtime 把任务标记为成功完成并清理资源。summary 字段是任务最终交付凭证。\n"
        "\n"
        "【何时使用】\n"
        "• 任务实际目标已达成\n"
        "• 且用户已经【明确确认】成果可以接受（验收通过）\n"
        "\n"
        "【不要用于】\n"
        "• 任务失败 → 必须用 fail_task。end_task 表达“成功”语义不能混用\n"
        "• 用户还没确认成果 → 先用 message_user(action=request_response) 汇报+等验收\n"
        "• 任务做到一半想暂停 → 用 pause_task 或 message_user(action=request_response)\n"
        "\n"
        "【必须的调用顺序】\n"
        "1. 完成任务实际工作\n"
        "2. 调 message_user(action=request_response) 汇报关键交付物，等用户验收\n"
        "3. 等到用户给出明确肯定反馈（如“通过”、“OK”、“完成”等）\n"
        "4. 调 end_task 终止任务\n"
        "\n"
        "不要在用户没确认的情况下，自己判断“做完了”就直接 end_task。"
    ),
    group="agent_lifecycle",
    category="lifecycle",
    always_enabled=True,
    allowed_loop_decisions=("complete",),
)
def end_task(request: EndTaskRequest) -> BaseToolResult:
    """结束任务并退出。"""
    return BaseToolResult(
        success=True,
        execution_status="success",
        output=request.summary,
        loop_control=LoopControl(
            decision="complete",
            reason=f"end_task: {request.summary}",
        ),
        metadata={"lifecycle_action": "end_task"},
    )


# ── Tool 4: fail_task ───────────────────────────────

class FailTaskRequest(BaseToolRequest):
    """报告任务失败工具请求。"""

    reason: str = Field(
        description=(
            "任务失败的根本原因和已尝试过的方案，会被记录到任务 trace 供后续追查。"
            "不会作为消息发送给用户——如果需要告知用户失败原因，请先用 message_user(action=notify) 通知后再调用 fail_task。"
        ),
    )


@tool(
    name="fail_task",
    description=(
        "任务【不可恢复失败】时调用——Agent 判断当前任务无法继续推进。结束 Agent 循环，任务进入失败终态，不可逆。\n"
        "\n"
        "【做什么】触发 loop_decision=fail 让 Runtime 把任务标记为失败并清理资源。reason 字段记录根本原因和已尝试的方案。\n"
        "\n"
        "【何时使用】\n"
        "• 缺必需的工具/权限/凭证，没有绕过的方案\n"
        "• 输入数据严重缺失或冲突，无法继续\n"
        "• 连续多次尝试相同方向均失败，且没有其他可行路径\n"
        "• Agent 自身能力范围外的任务（识别后承认无法完成）\n"
        "\n"
        "【不要用于】\n"
        "• 单个工具调用报错 → 框架已经把工具异常包装为 success=False 的 ToolResult，不需要主动 fail_task\n"
        "• 不确定下一步怎么做 → 先用 message_user(action=request_response) 求助用户，可能用户能帮你解开困局\n"
        "• 任务成功完成 → 用 end_task\n"
        "\n"
        "【推荐的调用顺序】\n"
        "1. 用 message_user(action=notify) 把失败原因和已尝试过的方案告知用户\n"
        "2. 调 fail_task 终止任务\n"
        "\n"
        "reason 必须说清楚根本原因和已尝试方案，会被记录到任务 trace 供事后追查。"
    ),
    group="agent_lifecycle",
    category="lifecycle",
    always_enabled=True,
    allowed_loop_decisions=("fail",),
)
def fail_task(request: FailTaskRequest) -> BaseToolResult:
    """报告任务失败并终止当前循环。"""
    return BaseToolResult(
        success=True,
        execution_status="success",
        output="Task marked as failed.",
        loop_control=LoopControl(
            decision="fail",
            reason=f"fail_task: {request.reason}",
        ),
        metadata={"lifecycle_action": "fail_task"},
    )
