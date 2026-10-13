# 任务 Note 工具模块（task_note）

负责面向后台观察者（Task Note Observer）的任务意图、宏观方案、决策、待办、认知真相、问题排查、交付产物、验证结论与恢复快照的专属维护工具集（共 36 个工具、16 张表）。

## 模块边界

本模块是具体业务工具模块，负责 Task Note 数据结构与专属维护工具的实现。

- `tool` 模块负责统一工具协议、注册表、Schema 生成与执行调度。
- `task_note` 工具必须保持 `group="task_note"` 供展示筛选，并配置 `category="business"`。
- **业务 Agent 严格隔离**：业务 Agent 在任何状态下工具列表中都不持有任何 Task Note 工具；Note 工具只挂载在后台独立的观察者会话上（D06）。
- 工具入参必须基于 `BaseToolRequest`，严禁在 Schema 中包含 `task_id`、`agent_instance_id` 等环境元数据参数，这些参数统一通过 `ctx: ToolRuntimeContext` 与 `ObserverBinding` 隐式注入（杜绝默认静态兜底 ID）。
- 出参遵循零冗余原则：
  - 严禁在 `output` 字典中包含 `"success": true`；
  - 严禁机械回显入参中的非主键字段；
  - 仅返回系统生成的记录主键 ID、简明对账标识文本以及极简的 message 结论。
- `note_update_end` 必须声明 `allowed_loop_decisions=("continue", "complete")`，三项准入校验通过后方可返回 `decision="complete"` 并推进断点水位。

## 双线程解耦架构与上下文替换规则

1. **双线程观察模型（前后台完全解耦，D02）**：
   - 前台业务 Agent 专注任务执行，不持有 Note 工具，不感知观察者存在；
   - 后台观察者是独立的 LLM 会话，自上一断点以来的消息增量达到阈值时（从配置读取 `observer_trigger_tokens`，默认 80k），由程序后台异步启动观察，业务 Agent 不阻塞；
   - 每次观察均为全新独立 Conversation，同一目标 Agent 同一时刻最多只有一个观察运行；
   - 观察会话通过 `ObserverBinding` 注入目标 Agent 身份与切片 units（D03, M5）。
2. **断点校验与水位推进（D04）**：
   - `note_update_end` 必须依次校验：
     1. covered_until_seq 属于本次增量 units 且覆盖比例达到配置要求（默认 >= 50%）；
     2. 该 seq 及之前的 auto_record 项均已绑定 mission_id 或已删除；
     3. 目标 Agent 每个 in_progress 状态的任务均有 progress 记录。
   - 全部通过后原子推进 `task_note_watermark`；任一不通过返回拒绝并详细说明缺失项，水位不推进。
3. **上下文热替换与尾部保留（D05, M6）**：
   - 仅在业务 Agent 两次 LLM 调用之间检查门禁；
   - 命中门禁时，新上下文 = 业务系统提示词 + 快照手记（user，只取已绑定行与卡片）+ `covered_until_message_id` 之后的原始残差消息；
   - 只有快照读取成功且 `note_version >= 1` 时才允许替换；
   - M6-1：替换时若后台观察运行中，等待最多 `swap_wait_timeout_seconds`（超时使用上一版快照）；
   - M6-2：组装后残差尾部若仍超门禁阈值，先同步执行一次观察再重新组装；
   - M6-3：残差尾部首条若是 user 消息，与快照 user 消息安全合并，确保 LLM 协议合法。
4. **存量升级兼容（M7）**：
   - 升级或重启时若检测到实例处于 `execution_substate="note_update"`，直接重置为业务态，保留 `pending_user_messages` 队列，水位不推进，按正常业务态执行；
   - 删除消息分流逻辑，所有待处理用户消息正常 drain 消费。
5. **降级与容错策略（D08）**：
   - PostgreSQL 存储不可用或连接失败时，Note 工具显式返回失败，不静默假成功；
   - 快照读取失败或为空时不替换上下文，业务会话保持原样，watermark 不推进。
6. **双 ID 租户环境隔离约束**：
   - Task Note 16 张表全量以 `task_id` 和 `agent_instance_id` 作为底层隔离归属字段；
   - 无论单表增删改查还是读取快照，所有 SQL 必须 100% 携带 `task_id` 与 `agent_instance_id` 双条件；
   - 方案与进度 UPSERT 冲突分支同步更新 `task_id = EXCLUDED.task_id`。
7. **生命周期垃圾回收与物理清理约束 (GC)**：
   - 当子 Agent 或 Root Agent 结束时，清理前先调用调度器等待或取消运行中的观察（最多 `swap_wait_timeout_seconds`）；
   - 随后物理清除全部 16 张 Note 表中该实例或 task 的记录，杜绝数据库残留膨胀。
8. **意图指令语义维护契约**：
   - `agent_user_mission.raw_prompts` 记录委派指令，允许 Agent 在保证原始核心含义和真实需求绝对不变的前提下优化措辞。
