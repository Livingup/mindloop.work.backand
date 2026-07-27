# Agent 生命周期控制模块（agent_lifecycle）

负责提供 Agent 生命周期与用户沟通的内置工具能力，通过统一工具执行协议把 Agent 的沟通意图和执行循环控制决策显式传递给 Runtime。

## 模块定位

`agent_lifecycle` 是独立业务模块，不把生命周期动作硬编码在 `AgentExecutionEngine` 中。它通过普通本地工具注册机制提供 4 个独立的生命周期工具，并依赖 `tool` 模块中的 `BaseToolRequest` / `BaseToolResult` / `LoopControl` / `FrontendMessage` 协议表达执行状态、前端消息和下一步循环决策。

按"单一职责 + LLM 语义清晰"原则，生命周期能力拆分为 4 个独立工具而不是一个聚合的工具：语义清晰，LLM 选择容易，每个工具的 `allowed_loop_decisions` 只包含该工具实际产出的决策。沟通与生命周期决策严格分离：`message_user` 只负责“发消息”，`pause_task` / `end_task` / `fail_task` 负责生命周期决策，不交叉。

## 四个独立工具

### 1. `message_user` — 向用户发送消息

向用户发送消息的统一工具。设计原则是“一个工具 + 两种行为模式”——只区分“是否需要等用户回复”，不把“消息语义”（提问/审核/汇报等）装进 action，语义由 message 文本本身表达。

| action | loop decision | 使用场景 |
|---|---|---|
| `notify` | `continue` | 单向同步消息，不阻塞。适用：阶段性进度报告、重要发现告知、耗时操作预告、关键状态广播 |
| `request_response` | `pause` | 发消息并阻塞等用户回复。覆盖所有需要用户回应的场景：提问、请求确认、请求选择、汇报成果等待审核、危险操作前人工确认、向用户求助等 |

请求模型 `MessageUserRequest`：

| 字段 | 类型 | 说明 |
|---|---|---|
| `action` | Literal | `notify` / `request_response` |
| `message` | str | 需要展示给用户的 HTML fragment 内容 |
| `text_format` | `html` | 文本格式声明，前端按 HTML fragment 渲染 |
| `options` | list[`MessageOption`] | `request_response` 使用的结构化选项数组，每项含 `id` / `label` / `description` |
| `channel` | str \| None | 目标前端通道，例如 web、cli，一般留空默认沿用会话流 |
| `metadata` | dict | 附加元数据 |

注册元数据：`name="message_user"`、`category="lifecycle"`、`always_enabled=True`、`allowed_loop_decisions=("continue", "pause")`。

### 2. `pause_task` — 暂停任务等待外部事件或超时

被动等待型暂停工具，不向用户发送消息。适用于"等待其他 Agent 消息""等待系统事件""等待外部回调"等场景。与 `message_user(action=request_response)` 的区别在于：`request_response` 是主动向用户发问/请求回应并暂停，`pause_task` 不发消息，只暂停等外部事件；即使设置了超时，也不能用来等待真实用户回复。

请求模型 `PauseTaskRequest`：

| 字段 | 类型 | 说明 |
|---|---|---|
| `reason` | str | 暂停原因，说明在等待什么事件或消息 |
| `timeout_seconds` | float | 等待已知委派结果或明确异步作业时使用 `>0` 的有限检查点；仅在本 Agent 工作全部完成、最终结果已向父 Agent/远程上游汇报、且没有已知待返回结果时使用 `-1` 无限期休眠；`0`、小于 `-1` 和非有限数非法 |

等待已知的本地子 Agent、远程 Agent 或明确异步作业结果时，调用方必须按复杂度设置正数检查点超时（常见约 5–50 分钟），不得使用 `-1`，也不得假设完成通知绝对可靠。检查点到期仍未收到结果时，先检查子/远程 Agent 或异步作业状态；若其 paused 或持续静默，主动询问是否完成，并请求进度、阻塞点和预计完成时间。

仅当当前 Agent 的受派工作已经全部完成、最终结果已向父 Agent 或远程上游汇报、且没有仍待返回的已知子任务/结果时，才使用 `timeout_seconds=-1` 进入无限期休眠等待不确定的未来指令；此时不要使用正数超时制造无意义的周期性唤醒。

注册元数据：`name="pause_task"`、`category="lifecycle"`、`always_enabled=True`、`allowed_loop_decisions=("pause",)`；返回 metadata 包含 `lifecycle_action="pause"`、`pause_timeout_seconds` 与 `pause_wake_mode`（`external_only` / `external_or_timeout`）。

### 3. `end_task` — 结束任务并退出（成功终止）

结束当前任务并关闭任务会话，仅用于任务成功完成。通常在用户确认任务已完成后调用（前面先用 `message_user(action=request_response)` 汇报成果等待验收）。任务失败请使用 `fail_task`，不要用 `end_task`。

请求模型 `EndTaskRequest`：

| 字段 | 类型 | 说明 |
|---|---|---|
| `summary` | str | 任务最终总结，简要说明完成情况和关键交付物 |

注册元数据：`name="end_task"`、`category="lifecycle"`、`always_enabled=True`、`allowed_loop_decisions=("complete",)`。

### 4. `fail_task` — 报告不可恢复失败并终止任务

宣告 Agent 自身无法继续推进任务并终止当前循环。与 `end_task` 平行，但表达完全不同的任务终态（失败 vs 成功）。适用：缺必需的工具/权限、输入数据严重不一致、连续多次尝试均失败且无法决策下一步。注意：常规工具异常会被框架自动捕获包装，不需要主动 `fail_task`。推荐先用 `message_user(action=notify)` 告知用户失败原因和已尝试的方案，再调用 `fail_task`。

请求模型 `FailTaskRequest`：

| 字段 | 类型 | 说明 |
|---|---|---|
| `reason` | str | 任务失败的根本原因和已尝试过的方案，会被记录到 trace |

注册元数据：`name="fail_task"`、`category="lifecycle"`、`always_enabled=True`、`allowed_loop_decisions=("fail",)`。

## 返回模型

所有生命周期工具都返回 `BaseToolResult`：

- `success=True`、`execution_status="success"`
- `output` 是简短的业务状态文本（如 `"Message delivered to user."`、`"Task paused, waiting for external event."`、`<summary>`）
- `loop_control.decision` 固定由工具语义决定（不依赖请求参数拼装）
- `message_user` 额外填充 `frontend_message`，包含前端可直接展示的 text、text_format、channel，以及 metadata 中的 action、options 结构化字段
- `metadata.lifecycle_action` 记录当前生命周期动作

Runtime 以 `BaseToolResult.loop_control.decision` 控制循环，不根据工具名或 action 硬编码分支；`pause_task` 的定时唤醒策略通过结构化 metadata（`pause_timeout_seconds` / `pause_wake_mode`）传递给 `AgentExecutionEngine`，不要从 `reason` 文本解析超时策略。

## 工具注册规则

四个工具都通过 `@tool` 装饰器注册为本地工具，共用 `group="agent_lifecycle"` 便于分组展示，且必须保持 `category="lifecycle"`。`LocalToolFactory.create()` 会 import `agent_runtime.agent_lifecycle.agent_lifecycle_tool` 触发注册，确保 Agent 执行器创建本地工具网关时这四个工具进入本地工具库。`AgentToolDispatcher` 会把 `category in {"lifecycle", "system"}` 的本地工具加入每个 Agent 的 LLM 工具列表，不要求每个 `AgentDefinition.tool_ids` 显式声明；`always_enabled` 只是由 category 派生的兼容展示字段。

## Runtime 协作规则

`AgentExecutionEngine` 会在系统提示词中注入 Runtime lifecycle tool policy，要求 LLM：

1. 面向用户的沟通必须调用 `message_user`，不要依赖普通 assistant 文本。
2. `message_user(action=notify)` 做非阻塞同步（进度报告、重要发现告知）；`message_user(action=request_response)` 做问题、确认、审核、选择、求助等需要用户回复的交互。不要用 notify 提问或表达“等用户回复”。
3. `pause_task` 不发消息。等待已知的本地子 Agent、远程 Agent 或明确异步作业结果时，必须使用正数检查点超时（常见约 5–50 分钟）；检查点到期未收到结果时检查状态，若 paused 或静默则主动询问是否完成，并请求进度、阻塞点和预计完成时间。只有当本 Agent 工作已全部完成、最终结果已向父 Agent/远程上游汇报且没有已知待返回结果时，才使用 `timeout_seconds=-1` 无限期休眠等待未来指令；不要用正数超时制造无意义周期唤醒。真实用户回复仍必须用 `message_user(action=request_response)`，不要用 pause_task。
4. 任务成功完成后用 `message_user(action=request_response)` 汇报成果并等待用户验收，得到明确肯定后再 `end_task`。未经用户确认不要直接 `end_task`。
5. 不可恢复失败用 `fail_task` 报告（不是 `end_task`）。推荐先 `message_user(action=notify)` 告知失败原因和已尝试的方案，再 `fail_task`。常规工具异常框架会自动包装，不需要主动 `fail_task`。
6. 不要在同一 assistant turn 中混用生命周期工具和业务工具，除非用户明确要求。
7. 必须显式设置 `text_format=html`，`message` 必须是 HTML fragment，不要返回 Markdown 或普通 assistant 文本。
8. HTML 输出应遵循 `message_user` 工具 schema：对比、范围、状态、计划或结果类信息优先用表格；段落列表最多三级，采用“无符号标题/段落 → 圆点主项 → 短横线细节”的视觉层级，少于三级即可表达清楚时不要强行凑满。
9. `request_response.options` 必须是包含 `id`、`label`、可选 `description` 的对象数组。

如果 LLM 连续返回普通文本而没有工具调用，Runtime 会把它视为 plain text fallback；连续达到 `_PLAIN_TEXT_FALLBACK_PAUSE_THRESHOLD` 后暂停实例和任务，避免无限循环。

## 目录结构

```text
agent_lifecycle/
├── __init__.py                    # 模块包入口
├── agent_lifecycle_tool.py        # 四个生命周期工具的实现（message_user / pause_task / end_task / fail_task）
└── AGENTS.md                      # 模块说明文档
```
