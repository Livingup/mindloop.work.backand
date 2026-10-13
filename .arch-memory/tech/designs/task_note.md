# task_note · 技术设计

## 双线程观察与前缀替换
- 方案：观察在后台独立会话运行（TaskNoteObserverScheduler 启动、TaskNoteObserverRunner 执行 LLM → Note 工具循环），业务 Agent 不阻塞；观察会话经 TaskNoteObserverBinding 绑定目标实例与 run_id
- 方案：替换由引擎在两次 LLM 调用之间发起，新上下文 = 系统提示词 + 快照消息 + 断点（无断点则前缀末条）之后的原始消息；尾部首条为 user 时合并为新消息，content[0] 为手记
- 方案：units 的 seq 仅为单次观察内的局部编号（从 1 起），断点一律以 unit.end_message_id 落盘；units 含 ToolUsePart 与 ToolResultPart，auto_record 从 ToolResultPart.output 读取
- 不变量：观察循环没有轮数上限，只在 note_update_end 校验通过时成功结束；LLM 请求失败即本次观察失败
- 不变量：TASK_NOTE-18 的等待不设超时
- 禁止：不得恢复原地切换（业务实例进入 note_update 子状态、挂 Note 工具、整体换新会话）；存量 execution_substate=note_update 的实例在加载时重置为业务态并保留 pending 消息
- 禁止：观察调度不得使用全局锁，锁内不得做 PG I/O

## 跟踪状态 tracking
- 方案：conversation_state.task_note_tracking（SQLite JSON）是覆盖位置与观察状态的唯一权威；字段 version、prefix_end_message_id、snapshot_message_id、covered_until_message_id、note_version、inherited_processed、observing{run_id, start_after_message_id, end_message_id, started_at}
- 方案：增量起点 = covered_until_message_id，为空取 prefix_end_message_id，再为空从首条起；快照消息只计 content[1:]
- 方案：TaskNoteTrackingCoordinator 是 tracking 的唯一推进点，每实例一把 asyncio.Lock，三个更新点：
  - U1 on_round（每轮 LLM 返回后与工具批次后）：observing 为空且增量≥observer_trigger_tokens 时写 observing 并交出消息拷贝
  - U2 apply_swap：先 await_running，再替换；替换后 prefix_end/snapshot_message_id 指向新快照消息，covered_until_message_id 清空
  - U3 complete：run_id 与 observing.run_id 一致才生效，写断点、note_version+1、inherited_processed=true、observing 清空；不一致返回 STALE_OBSERVATION
- 不变量：覆盖位置只由 U3 推进；观察失败只清 observing
- 不变量：tracking 只经 cas_task_note_tracking（WHERE version=?）更新，CAS 失败重读重算一次，再失败记 error 不推进
- 不变量：子 Agent 创建时以边界消息 ID 作为 fork_message_id 初始化 tracking：prefix_end_message_id=边界 ID、inherited_processed=false、note_version=0；根 Agent inherited_processed 恒为 true
- 禁止：save(conv) 不得写 task_note_tracking 列
- 禁止：调度器与组装器不得写覆盖位置；PG task_note_watermark 表不得再写入，不得作为覆盖位置来源

## Note 存储
- 方案：Note 数据存 PostgreSQL，复用 cloud_user_config_database 连接；所有查询按 task_id + agent_instance_id 双条件隔离
- 方案：服务启动时由 server lifespan 调用 ensure_task_note_schema 执行一次幂等 DDL 并断言迁移列，失败则标记 Note 不可用、服务照常启动
- 方案：Note 与 tracking 不同事务；U3 写 tracking 失败时该段重新观察，Note 工具按主键 upsert 保证幂等
- 不变量：TASK_NOTE-19 下 TaskNoteEventRecorder 仍静默容错，不得让业务工具因记录失败而失败
- 不变量：task_artifact_records.type 新写入取 file_create/file_modify/file_delete/file_move/other，读取兼容无前缀旧值
- 禁止：启动链路不得执行 INSERT/UPDATE/DELETE；task_command_records 不得持有 mission_id，bind_resources_to_mission 只接受 artifact、agent_relationship

## 提示词与推送
- 方案：观察者提示词唯一正本是 Runtime prompt snippet system_note_update，经模板网关读取；/api/context-compression/observer-prompt 读正本；快照只经 NOTE_SNAPSHOT_JSON 一个通道注入
- 方案：task_note.updated 由 runner 在观察者 Note 工具成功后推给目标 Agent；lacp 入站自行发布
- 方案：GET /tasks/{task_id}/agents/{agent_instance_id}/note 的 watermark 读 tracking：covered_until_seq 固定 0，covered_until_message_id、note_version、observing 取真值；存储不可用返回 503
- 不变量：Agent 结束清理 Note 前先取消或等待其观察；子 Agent 按 task_id + agent_instance_id 清理，根 Agent 按 task_id 清理
- 禁止：不得在仓库文件、YAML、PG 各自维护 system_note_update 正文；模板只经 prompt snippet REST 写入
