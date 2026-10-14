# Task Note 上下文压缩 task_note

## 边界
- 负责：压缩门禁、按实例增量触发观察、观察会话、观察者模型选择、Note 数据（16 张表、36 个 Note 工具、事实写入接口）、task_note_tracking 的结构与更新规则、替换内容组装、手记渲染、Note 清理、压缩配置、task_note.updated 的发布口径、/note 只读视图
- 不负责：Agent 循环与调用时机（→ agent 执行引擎）、通用工具分发（→ tool，分发器在工具成功后按本域口径发布 task_note.updated）、conversation_state 存储（→ conversation_message）、事件通道与 SSE（→ communication）、子会话创建与边界消息生成（→ subagent）、远程传输（→ lacp）、模板存储（→ prompt_template）、LLM 调用（→ llm_client）

## 关键概念
- 班次 task_id：从启动 Agent 到 end_task 的会期，一个班次内可先后或并行发生多个 Mission
- Mission：班次内的一项业务目标，是 Note 记录的聚合锚点
- Note：Agent 在某一断点继续工作所需的最小确定性状态快照，不是对话摘要
- 语义记录：由观察者经 Note 工具写入的 11 张表（agent_user_mission 与 10 张主题表）
- 事实记录：程序钩子在工具执行成功后写入的行，mission_id 初始为空，等待观察者绑定
- 观察会话：为某个业务 Agent 实例后台新开的独立 LLM 会话，只挂 Note 工具，只由 note_update_end 校验通过结束
- 观察者：观察会话中的 LLM，按目标 Agent 的增量消息提炼 Note
- 断点：观察实际覆盖到的最后一条消息，以 message_id 表示
- tracking：conversation_state.task_note_tracking，记录本实例的前缀、快照消息、断点、note_version、继承段是否已处理、运行中的观察
- 快照消息：替换后作为首条 user 消息的手记；尾部首条为 user 时与之合并
- 继承段：子 Agent 会话中边界消息及之前复制自父辈的前缀，只作背景
- 压缩门禁阈值：模型 compression_threshold，缺省为 context_window × default_threshold_ratio；按 last_context_usage 加之后新增消息的估算判断，没有 usage 时不压缩
- 观察触发阈值：observer_trigger_tokens，与断点之后的消息估算比较
- 推进比例：observer_min_advance_ratio，断点 seq 及之前的 units 占本次 units 的比例
- 快照标题：快照消息首个文本块的固定前缀“# 任务手记快照”

## 状态流转
观察（每实例）：无观察 →(增量达到触发阈值) 观察中 →(note_update_end 通过) 无观察，断点推进
观察（每实例）：观察中 →(观察失败 / 重启后 run_id 失效 / Agent 结束时取消) 无观察，断点不变
上下文：原始会话 →(达到压缩门禁阈值，等本实例观察结束) 命中门禁 →(note_version=0 或断点后增量 ≥ 门禁阈值) 同步观察 → 替换
上下文：命中门禁 →(note_version≥1) 系统提示词 + 快照消息 + 断点之后的原始消息
Mission：pending / in_progress →(TASK_NOTE-09 的完成条件) completed
Mission：pending / in_progress →(用户明确放弃) abandoned
Mission：completed / abandoned →(裁剪) 已裁剪

## 需求约束
- TASK_NOTE-01 业务 Agent 不得持有任何 Note 工具，Note 表仅限观察者与程序钩子写入。
- TASK_NOTE-02 程序自动记录的 Note 行仅限：文件工具的增删改移、子 Agent 创建与释放及委派关系、父子 Agent 专线消息、远程 Agent 消息。
- TASK_NOTE-03 task_command_records 仅作 command_id 到命令行的引用索引，不属于 Note 内容。
- TASK_NOTE-04 观察者对程序自动记录的行不得重复创建或改写内容，仅限绑定 mission_id 或按行主键删除。
- TASK_NOTE-05 Mission 必须平铺，不得维护任务间父子关系；Agent 协作拓扑仅由 task_agent_relationships 记录。
- TASK_NOTE-06 除 agent_user_mission 与 task_progress_content 外，Note 主题记录没有对话或工具证据时不得创建。
- TASK_NOTE-07 Note 工具中需要归属任务的调用必须显式传入 mission_id，不得提供缺省值。
- TASK_NOTE-08 Note 工具入参不得包含 task_id、scene_id、agent_instance_id；写入时由 Runtime 注入目标 Agent 的 agent_instance_id，观察者仅限修改目标 Agent 的记录。
- TASK_NOTE-09 Mission 仅限在用户确认、委派方确认，或结果已交付且对方转向其他话题时置为 completed；Agent 自称完成不算。
- TASK_NOTE-10 已裁剪的 Mission 只读，不得再写入主题记录。
- TASK_NOTE-11 进行中 Mission 的 decision、issue、validation、external_state 记录不得物理删除，仅限软废弃或闭环；issue 闭环必须填写 resolution。
- TASK_NOTE-12 task_progress_content 与 task_solution_content 在同一 Mission、同一 Agent 实例下必须只有一份。
- TASK_NOTE-13 组装快照时仅限取 mission_id 已绑定的行。
- TASK_NOTE-14 观察者不得执行目标 Agent 的任务：不得编写业务内容、搜索网络、执行命令、调用业务 MCP 工具，不得向用户或其他 Agent 发消息。
- TASK_NOTE-15 note_update_end 仅限在以下条件同时满足时通过：断点 seq 属于本次 units；断点 seq 及之前的 units 占比不低于 observer_min_advance_ratio；断点 unit 带消息 ID；断点之前的 auto_record 行均已绑定或删除；每个 in_progress Mission 都有 progress。
- TASK_NOTE-16 同一业务 Agent 实例同一时刻最多一个观察在运行；不同实例的观察互不影响，可并行。
- TASK_NOTE-17 观察失败时断点不得推进，下次从原断点重新观察。
- TASK_NOTE-18 替换前缀时，若本实例有观察在运行，必须等其结束；note_version 为 0 或断点后增量不低于门禁阈值时，必须先同步完成一次观察再替换；同步观察后 note_version 仍为 0 时不得替换。
- TASK_NOTE-19 Note 存储不可用时 Note 工具必须返回 TASK_NOTE_STORAGE_UNAVAILABLE，快照读取失败时不得替换上下文、不得推进断点。
