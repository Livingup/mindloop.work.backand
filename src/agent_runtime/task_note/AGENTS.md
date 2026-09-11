# 任务 Note 工具模块（task_note）

负责面向 Note 更新 Agent 的任务意图、宏观方案、决策、待办、认知真相、问题排查、交付产物、验证结论与恢复快照的专属维护工具集（共 32 个工具）。

## 模块边界

本模块是具体业务工具模块，负责 Task Note 数据结构与专属维护工具的实现。

- `tool` 模块负责统一工具协议、注册表、Schema 生成与执行调度。
- `task_note` 工具必须保持 `group="task_note"` 供展示筛选，并配置 `category="business"`；在系统进入 Note 更新状态时由 Runtime 原地挂载。
- 工具入参必须基于 `BaseToolRequest`，严禁在 Schema 中包含 `task_id`、`agent_instance_id` 等环境元数据参数，这些参数统一通过 `ctx: ToolRuntimeContext` 隐式注入。
- 出参遵循零冗余原则：
  - 严禁在 `output` 字典中包含 `"success": true`；
  - 严禁机械回显入参中的非主键字段；
  - 仅返回系统生成的记录主键 ID、简明对账标识文本以及极简的 message 结论。
- `note_update_end` 必须声明 `allowed_loop_decisions=("continue", "complete")`，返回 `decision="complete"` 作为退出 Note 更新状态的控制信号。

## Note 上下文压缩与状态流转规则

1. **零 Agent Fork（Zero Agent Fork）**：
   - 上下文压缩进入 Note 更新时不创建新 Agent 实例，`agent_instance_id` 全程保持不变。
   - 通过将业务 Agent 原地切换为 Note 更新状态（`execution_substate="note_update"`）实现。
2. **状态持久化与抗中断**：
   - `AgentInstance.execution_substate` 在进入 Note 更新时持久化到 SQLite `agent_instances.execution_substate`，退出 Note 更新时清空为 `None`。
   - 任务中断、暂停或进程重启后，恢复执行时若处于 `note_update` 状态，必须直接恢复 Note System Prompt 和 Note Tools，绝不发生状态丢失或提示词/工具撕裂。
3. **提示词模板化与工具权限隔离**：
   - Note 系统提示词必须从模板网关读取 `name="system_note_update"`（支持 `{{agent_instance_id}}` 和 `{{available_tools}}` 占位符），不得硬编码。
   - 在 `note_update` 状态下执行循环强制隔离工具，仅允许调用 32 个 Task Note 工具，非 Note 工具严格拦截并返回工具错误。
4. **触发与退出时序**：
   - 触发时序：查询 PG 基线快照 -> 追加边界 User 提示消息 -> 更新 `conversation.timeline`（冻结业务 prompt/tools 为 historical，追加 Note active 配置事件）并保存会话 -> `enter_note_update()` 持久化状态 -> 下发 Note Prompt/Tools。
   - 退出时序：调用 `note_update_end` 工具 -> 查询 PG 最新完整快照 -> 创建全新干净 `Conversation` 并以快照为初始消息注入 -> timeline 初始化为业务 prompt/tools -> `switch_agent_instance_conversation` -> `exit_note_update()` 持久化状态 -> 覆盖 `loop_decision="continue"` 让业务 Agent 在新会话中无缝继续执行。
