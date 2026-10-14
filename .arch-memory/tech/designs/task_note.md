# task_note · 技术设计

## 双线程观察与前缀替换
- 方案：观察在后台独立会话运行（TaskNoteObserverScheduler 启动、TaskNoteObserverRunner 执行 LLM → Note 工具循环），业务 Agent 不阻塞；观察会话经 TaskNoteObserverBinding 绑定目标实例与 run_id
- 方案：触发阈值 T = min(observer_trigger_tokens, 门禁阈值 × 0.5)；observer_trigger_tokens、observer_min_advance_ratio、default_threshold_ratio 均由配置构造器从 YAML 读入 ContextCompressionConfig，默认 80,000、0.5、0.75
- 方案：门禁按约 4 字符 1 token 估算；切片与触发按中日韩自适应估算（中文约 1.2 token/字，其他约 1 token/3.5 字符）；80k 增量不用 usage，不计系统提示词与工具 Schema
- 方案：每次观察新建仅在内存的 conv_obs_* 会话，首条 user 消息含目标身份（父 ID、祖先链）、目标 Agent 系统提示词、基准快照（去掉 commands）、继承段（子 Agent 且 inherited_processed=false 时）、增量 units；LLM 未调用工具时追加提醒后继续，max_worker_rounds 对观察者不生效
- 方案：观察者模型依次取目标 Agent 执行策略首个候选、目标 Agent 的 provider/binding、第一个执行策略
- 方案：替换由引擎在两次 LLM 调用之间发起，新上下文 = 系统提示词 + 快照消息 + 起算范围；尾部首条为 user 时合并为新消息并沿用其 ID，content[0] 为手记
- 方案：替换先保存会话消息，再执行 U2 写 tracking，两步不在同一事务；随后立刻重置 usage 基线 = 估算(系统提示词 + 工具 + 新消息)、记录点 = 新消息数，failover 候选的门禁按新基线判断（K2）
- 方案：LLM 成功响应用真实 usage 覆盖基线；响应不带 usage 时用全量估算作基线
- 方案：units 的 seq 仅为单次观察内的局部编号（从 1 起），断点一律以 unit.end_message_id 落盘；units 含 ToolUsePart 与 ToolResultPart，auto_record 从 ToolResultPart.output 读取
- 不变量：观察只在 note_update_end 校验通过时成功结束，上限见 TASK_NOTE-20；其他任何结局（LLM 失败、轮数用完、异常、取消、准备阶段出错、STALE）均为观察失败
- 不变量：TASK_NOTE-18 的等待不设超时，由 TASK_NOTE-20 保证结束
- 不变量：签发 ticket 之后，从切片、读快照到观察循环结束整体包在一个 try/finally 中，必须有成功或失败收尾（K4）
- 不变量：快照消息首个文本块必须以“# 任务手记快照”开头；起点消息 ID 不在当前消息中时，统一回退到最后一条以此开头的消息
- 不变量：tracking.observing 的 run_id 不在进程内完成事件表时视为重启遗留，在 U1 或替换前等待时清除
- 禁止：不得恢复原地切换（业务实例进入 note_update 子状态、挂 Note 工具、整体换新会话）；存量 execution_substate=note_update 的实例在加载时重置为业务态并保留 pending 消息
- 禁止：观察调度不得使用全局锁，锁内不得做 PG I/O

## 跟踪状态 tracking
- 方案：conversation_state.task_note_tracking（SQLite JSON）是覆盖位置与观察状态的唯一权威；字段 version、prefix_end_message_id、snapshot_message_id、covered_until_message_id、note_version、inherited_processed、observing{run_id, start_after_message_id, end_message_id, started_at}
- 方案：增量起点 = covered_until_message_id，为空取 prefix_end_message_id，再为空从首条起；计数、观察切片、替换保留尾部三处统一按起算范围取消息（含起点消息中合并的原文）
- 方案：TaskNoteTrackingCoordinator 是 tracking 的唯一推进点，每实例一把 asyncio.Lock，三个更新点：
  - U1 on_round（每轮 LLM 返回后与工具批次后）：observing 为空且起算范围估算 ≥ T 时写 observing（终点冻结为当前末条）并交出消息拷贝
  - U2 apply_swap：先 await_running，再替换；替换后 prefix_end/snapshot_message_id 指向新快照消息，covered_until_message_id 清空
  - U3 complete：run_id 与 observing.run_id 一致才生效，写断点、note_version+1、inherited_processed=true、observing 清空；不一致返回 STALE_OBSERVATION
- 不变量：覆盖位置只由 U3 推进；观察失败只清 observing
- 不变量：tracking 只经 cas_task_note_tracking（WHERE version=?）更新；U1 CAS 失败重读重试一次；U3 CAS 失败时重读，run_id 仍为本次观察则重写，最多 3 次，仍失败按观察失败处理；U2 CAS 失败不重试，下一轮起点由快照标题回退确定
- 不变量：子 Agent 创建时以边界消息 ID 作为 fork_message_id 初始化 tracking：prefix_end_message_id=边界 ID、inherited_processed=false、note_version=0；写入失败重试 1 次，仍失败则创建子 Agent 失败并删除已复制的会话；根 Agent inherited_processed 恒为 true
- 禁止：save(conv) 不得写 task_note_tracking 列
- 禁止：调度器与组装器不得写覆盖位置；PG task_note_watermark 表不得再写入，不得作为覆盖位置来源

## Note 存储
- 方案：Note 数据存 PostgreSQL，复用 cloud_user_config_database 连接；所有查询按 task_id + agent_instance_id 双条件隔离
- 方案：服务启动时由 server lifespan 调用 ensure_task_note_schema 执行一次幂等 DDL 并断言迁移列，失败则标记 Note 不可用、服务照常启动
- 方案：Note 与 tracking 不同事务；U3 写 tracking 最终失败时该段重新观察
- 不变量：替换与观察读取快照失败时必须抛错，不得返回空骨架；替换路径不替换、照常请求，观察路径按失败处理（TASK_NOTE-19，K3）
- 不变量：TASK_NOTE-19 下 TaskNoteEventRecorder 仍静默容错，不得让业务工具因记录失败而失败
- 不变量：task_artifact_records.type 新写入取 file_create/file_modify/file_delete/file_move/other，读取兼容无前缀旧值
- 禁止：启动链路不得执行 INSERT/UPDATE/DELETE；task_command_records 不得持有 mission_id，bind_resources_to_mission 只接受 artifact、agent_relationship

## 提示词与推送
- 方案：观察者提示词唯一正本是 Runtime prompt snippet system_note_update，经模板网关读取；/api/context-compression/observer-prompt 读正本，失败时仍返回 200 且 prompt_text 以“# 读取提示词失败”开头
- 方案：快照 JSON 只经观察会话首条 user 消息注入；系统提示词中的 NOTE_SNAPSHOT_JSON 只渲染为指向该消息的固定说明
- 方案：task_note.updated（payload task_id、agent_instance_id、scene_id、tool_name、updated_at、note_version?；发往 stream-scene-{scene_id}，无 scene 时 stream-agent-task-{task_id}）表示该实例 /note 已过期，发布方三处：
  - runner：观察者 Note 工具成功后发布；note_update_end 成功时发布两次，第二次在 U3 清除 observing 之后并带 note_version
  - tool 分发器：业务工具（文件增删改移、终端命令、子 Agent 创建与释放、专线消息）或 Note 工具成功后，发给调用方及子 Agent、目标 Agent 或被释放的实例
  - lacp：入站时发给接收方
- 方案：观察开始、观察失败、被取消、STALE_OBSERVATION 均不发布 task_note.updated
- 方案：替换完成后发布 agent.task_note.context_swapped（payload task_id、agent_instance_id、conversation_id、note_version、snapshot_message_id）
- 方案：GET /api/agent-runtime/tasks/{task_id}/agents/{agent_instance_id}/note 契约字段为 task_id、agent_instance_id、watermark.covered_until_message_id、watermark.note_version、observing、snapshot（仅 mission_id 已绑定行）、markdown；404 detail 含 AGENT_INSTANCE_NOT_FOUND，任何读取失败返回 503 detail 含 TASK_NOTE_STORAGE_UNAVAILABLE
- 方案：/api/context-compression/settings 读写 {enabled, llm_policy_group_id, updated_at}，PUT 体为 {llm_policy_group_id}
- 方案：context_compression.* 事件不再发布
- 不变量：Agent 结束清理 Note 前先取消或等待其观察；子 Agent 按 task_id + agent_instance_id 清理，根 Agent 按 task_id 清理
- 禁止：/note 的 mission_id 查询参数与回显、watermark.covered_until_seq、watermark.updated_at、unbound 不属于契约，前端不得依赖
- 禁止：不得在仓库文件、YAML、PG 各自维护 system_note_update 正文；模板只经 prompt snippet REST 写入
