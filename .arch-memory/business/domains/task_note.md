# Task Note task_note

## 关键概念
- 班次 task_id：从启动 Agent 到 end_task 的会期，一个班次内可先后或并行发生多个 Mission
- Mission：班次内的一项业务目标，是 Note 记录的聚合锚点
- Note：Agent 在某一断点继续工作所需的最小确定性状态快照，不是对话摘要
- 语义记录：由 Note 维护方经 Note 工具写入的 11 张表（agent_user_mission 与 10 张主题表）
- 事实记录：程序钩子在工具执行成功后写入的行，mission_id 初始为空，等待绑定

## 状态流转
Mission：pending / in_progress →(TASK_NOTE-09 的完成条件) completed
Mission：pending / in_progress →(用户明确放弃) abandoned
Mission：completed / abandoned →(裁剪) 已裁剪

## 需求约束
- TASK_NOTE-01 业务 Agent 不得持有任何 Note 工具，Note 表仅限 Note 维护方与程序钩子写入。
- TASK_NOTE-02 程序自动记录的 Note 行仅限：文件工具的增删改移、子 Agent 创建与释放及委派关系、父子 Agent 专线消息、远程 Agent 消息。
- TASK_NOTE-03 task_command_records 仅作 command_id 到命令行的引用索引，不属于 Note 内容。
- TASK_NOTE-04 Note 维护方对程序自动记录的行不得重复创建或改写内容，仅限绑定 mission_id 或按行主键删除。
- TASK_NOTE-05 Mission 必须平铺，不得维护任务间父子关系；Agent 协作拓扑仅由 task_agent_relationships 记录。
- TASK_NOTE-06 除 agent_user_mission 与 task_progress_content 外，Note 主题记录没有对话或工具证据时不得创建。
- TASK_NOTE-07 Note 工具中需要归属任务的调用必须显式传入 mission_id，不得提供缺省值。
- TASK_NOTE-08 Note 工具入参不得包含 task_id、scene_id、agent_instance_id；写入时由 Runtime 注入目标 Agent 的 agent_instance_id，Note 维护方仅限修改目标 Agent 的记录。
- TASK_NOTE-09 Mission 仅限在用户确认、委派方确认，或结果已交付且对方转向其他话题时置为 completed；Agent 自称完成不算。
- TASK_NOTE-10 已裁剪的 Mission 只读，不得再写入主题记录。
- TASK_NOTE-11 进行中 Mission 的 decision、issue、validation、external_state 记录不得物理删除，仅限软废弃或闭环；issue 闭环必须填写 resolution。
- TASK_NOTE-12 task_progress_content 与 task_solution_content 在同一 Mission、同一 Agent 实例下必须只有一份。
- TASK_NOTE-13 组装快照时仅限取 mission_id 已绑定的行。
- TASK_NOTE-14 Note 维护方不得执行目标 Agent 的任务：不得编写业务内容、搜索网络、执行命令、调用业务 MCP 工具，不得向用户或其他 Agent 发消息。
