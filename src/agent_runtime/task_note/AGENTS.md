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
