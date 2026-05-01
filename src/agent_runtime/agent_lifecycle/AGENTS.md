# Agent 生命周期控制模块（agent_lifecycle）

负责提供 Agent 生命周期与用户沟通的内置工具能力，通过统一工具执行协议把 Agent 的沟通意图和执行循环控制决策显式传递给 Runtime。

## 模块定位

`agent_lifecycle` 是独立业务模块，不把生命周期动作硬编码在 `AgentExecutionEngine` 中。它通过普通本地工具注册机制提供 `agent_lifecycle` 工具，并依赖 `tool` 模块中的 `BaseToolRequest` / `BaseToolResult` / `LoopControl` / `FrontendMessage` 协议表达执行状态、前端消息和下一步循环决策。

## 对外能力

1. **用户沟通** — 通过 `notify` 和 `ask_user` 向用户或前端发送消息。
2. **任务暂停** — 通过 `ask_user` 或 `pause` 返回 `loop_control.decision="pause"`，让 Runtime 暂停实例和任务等待后续唤醒。
3. **任务完成** — 通过 `complete` 返回 `loop_control.decision="complete"`，让 Runtime 完成实例和任务。
4. **任务终止** — 通过 `terminate` 返回 `loop_control.decision="terminate"`，让 Runtime 取消当前任务。
5. **任务失败** — 通过 `fail` 返回 `loop_control.decision="fail"`，让 Runtime 标记任务失败并记录原因。

## 生命周期动作

| action | loop decision | 使用场景 |
|---|---|---|
| `notify` | `continue` | 向用户同步进度、提醒重要信息，但不阻塞后续执行 |
| `ask_user` | `pause` | 向用户提问、请求确认、请求选择，等待用户反馈后继续 |
| `pause` | `pause` | 主动暂停任务，等待用户、系统或其他 Agent 唤醒 |
| `complete` | `complete` | 任务完成后汇报结果并结束当前执行循环 |
| `terminate` | `terminate` | 用户要求结束或系统提示词允许主动终止任务 |
| `fail` | `fail` | 遇到不可恢复错误、死循环或无法继续的问题 |

## 请求模型

`AgentLifecycleRequest` 继承 `BaseToolRequest`，包含以下业务字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `action` | Literal | 生命周期动作，支持 `notify` / `ask_user` / `pause` / `complete` / `terminate` / `fail` |
| `message` | str | 需要展示给用户或记录到生命周期事件中的文本内容 |
| `text_format` | `plain_text` / `markdown` / `json` | `message` 的文本格式声明，前端按该字段渲染 |
| `options` | list[`AgentLifecycleOption`] | `ask_user` 使用的结构化选项数组 |
| `channel` | str 或 None | 目标前端通道，例如 web、cli |
| `metadata` | dict | 生命周期事件附加元数据 |

`AgentLifecycleOption` 是结构化选项对象，字段包括：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | str | 稳定选项标识，供前端回传用户选择 |
| `label` | str | 展示给用户的选项文本 |
| `description` | str 或 None | 可选的选项补充说明 |

## 返回模型

`agent_lifecycle` 返回 `BaseToolResult`：

- `success=True`
- `execution_status="success"`
- `output` 包含 action、message、text_format、options、channel、metadata
- `loop_control.decision` 由 action 映射得到
- `frontend_message` 包含前端可直接展示的 text、text_format、channel 和结构化 metadata
- `metadata.lifecycle_action` 记录当前生命周期动作

Runtime 只消费 `BaseToolResult.loop_control.decision` 控制循环，不根据工具名或 action 硬编码分支。

## 工具注册规则

`agent_lifecycle` 使用 `@tool` 注册为本地工具：

- `name="agent_lifecycle"`
- `group="agent_lifecycle"`
- `category="lifecycle"`
- `always_enabled=True`
- `allowed_loop_decisions=("continue", "pause", "complete", "terminate", "fail")`

`LocalToolFactory.create()` 会 import `agent_runtime.agent_lifecycle.agent_lifecycle_tool` 触发注册，确保 Agent 执行器创建本地工具网关时该工具已默认可用。`AgentToolDispatcher` 会把 `always_enabled=True` 的本地工具加入每个 Agent 的 LLM 工具列表，不要求每个 `AgentDefinition.tool_ids` 显式声明。

## Runtime 协作规则

`AgentExecutionEngine` 会在系统提示词中注入 Runtime lifecycle tool policy，要求 LLM：

1. 面向用户的沟通优先调用 `agent_lifecycle`，不要依赖普通 assistant 文本。
2. 使用 `notify` 做非阻塞进度同步。
3. 使用 `ask_user` 做问题、确认、审核、选择等需要用户反馈的交互。
4. 使用 `pause` 等待外部唤醒。
5. 使用 `complete` 汇报完成。
6. 使用 `terminate` 表示任务取消。
7. 使用 `fail` 表示不可恢复失败。
8. 不要在同一 assistant turn 中混用生命周期沟通工具和业务工具，除非用户明确要求。
9. 必须显式设置 `text_format`，不要返回 HTML。
10. `ask_user.options` 必须是包含 `id`、`label`、可选 `description` 的对象数组。

如果 LLM 连续返回普通文本而没有工具调用，Runtime 会把它视为 plain text fallback；连续达到 `_PLAIN_TEXT_FALLBACK_PAUSE_THRESHOLD` 后暂停实例和任务，避免无限循环。

## 目录结构

```text
agent_lifecycle/
├── __init__.py                    # 模块包入口
├── agent_lifecycle_tool.py        # agent_lifecycle 内置工具实现
└── AGENTS.md                      # 模块说明文档
```
