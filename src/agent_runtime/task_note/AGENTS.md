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
4. **触发时序**：
   - 触发时序：查询 PG 基线快照 -> 追加边界 User 提示消息 -> 更新 `conversation.timeline`（冻结业务 prompt/tools 为 historical，追加 Note active 配置事件）并保存会话 -> `enter_note_update()` 持久化状态 -> 下发 Note Prompt/Tools。
5. **退出时序与上下文反查重建**：
   - 调用 `note_update_end` 工具 -> 查询 PG 最新完整快照 -> 反查本地磁盘源码（`path + line_ranges` 带 1-based 行号）、反查命令行执行记录（`command_id` 映射到真实 `command_line`，标注为“命令行执行 ID”）、提取父子协作专线与远程 Agent 对等通信流 -> 组织为高密度结构化 Markdown 现场工作手记（突出 Next Step 与 Todo 复选框，严禁直接倾倒数据库表原始 JSON） -> 创建全新干净 `Conversation` 并以 Markdown 手记为初始消息注入 -> timeline 初始化为业务 prompt/tools -> `switch_agent_instance_conversation` -> `exit_note_update()` 持久化状态 -> 覆盖 `loop_decision="continue"` 让业务 Agent 在新会话中无缝继续执行。
6. **辅助表与索引增效约束**：
   - `task_command_records` 是纯技术辅助表，无 `mission_id` 字段，不归属业务任务，也不在 Note Agent 状态快照中直接倾倒。Note Agent 仅在业务表（workspace_context, validations, discoveries 等）中记录 `command_id` 作为命令行执行 ID，以极大节约上下文 Token；切回业务状态时由系统自动反查真实命令行指令。
7. **生命周期垃圾回收与物理清理约束 (GC)**：
   - Note 体系本质是服务于超长会话上下文压缩的“运行态工作记忆”，不是业务持久化或历史审计归档库。
   - 当非 Root Agent（子 Agent）调用 `end_task` 退出时，Runtime 必须物理清除该子 Agent 实例在全部 15 张 Note 表中的记录（`WHERE task_id = %s AND agent_instance_id = %s`）。
   - 当 Root Agent（主 Agent）调用 `end_task` 完成全任务时，Runtime 必须物理清除该 `task_id` 下全部 15 张 Note 表的记录（`WHERE task_id = %s`）。
   - 高频技术辅助表 `task_command_records` 必须随同上述 15 张表一起物理删除，杜绝命令累积导致数据库膨胀。
   - 清理动作由 Runtime 引擎静默执行，不得触发 LLM 对话或进入 Note 更新循环。
8. **产物格式化真实契约约束**：
   - `task_artifact_records.edits` 的底层存储仅包含 `feature_desc` 和 `replace_lines`（无原始删除行记录）。在反查与 Markdown 格式化手记时，严禁虚构带 `-` 删减行的伪 Git Diff，必须真实渲染 `feature_desc` 与 `replace_lines` 代码块。
