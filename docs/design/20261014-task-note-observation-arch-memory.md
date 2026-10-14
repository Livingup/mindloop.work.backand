# 20261014-task-note-observation-arch-memory

## 0. 元信息
- 仓库：agentruntime master @ c31db33b3b09560e27d1e5ab78212de67e8cad6b（另有工作区未提交改动：src/agent_runtime/task_note/task_note_tracking_coordinator.py、tests/test_task_note_tracking_coordinator.py，本设计以工作区为准，见 D1）
- 仓库：agent-frontend master @ 19d4db6d78dbc7f7b51ec76617192217f57e7bab
- 设计阶段：后端设计、前端设计（均为现状解读，用于补全架构记忆）；跳过 UI 设计（界面与交互不变）；未经 Scout 召回，设计师各自读源码
- 设计档位：后端 轻设计；前端 轻设计
- 用户确认记录：
  - 后端定稿（零~六节与冲突清单）：用户原话“通过”
  - 后端定稿变更（第四节接口契约、冲突/已知问题，处理前端转来的 3 条问题）：用户原话“通过”
  - 前端定稿（一~五节，含 TASK_NOTE-N1~N8）：用户原话“通过”

## 1. 需求原文
当前项目中task note更新和上下文观察功能的实现架构记忆不完整，需要补充

## 3. 业务架构（后端设计师定稿原文）
task_note 域，修改：
边界补充：
- 负责：观察者模型选择；事实记录与观察完成后的 task_note.updated 发布口径；/note 只读视图。
- 不负责：通用工具分发（→ tool）。但分发器在业务工具成功后会按本域口径发布 task_note.updated。
关键概念补充：
- 压缩门禁阈值：模型 compression_threshold，没有就取 context_window × default_threshold_ratio；按 last_context_usage 加上之后新增消息的估算来判断；没有 usage 时不压缩。
- 观察触发阈值：observer_trigger_tokens。
- 推进比例：observer_min_advance_ratio，指断点 seq 及之前的 units 占本次 units 的比例。
- 快照标题：快照消息首个文本块的固定前缀“# 任务手记快照”。
状态流转补充：
- 观察（每实例）：观察中 →(重启后 run_id 失效 / Agent 结束时取消) 无观察，断点不变。
- 上下文：命中门禁 →(note_version=0 或断点后增量 ≥ 门禁阈值) 同步观察 → 替换。
需求约束：改写 TASK_NOTE-15、TASK_NOTE-18（见第 5 节“既有约束变更”），不新增编号。

## 4. 接口契约（后端设计师定稿变更后原文）
三条问题的判定：
1. watermark.updated_at 恒为 null → 契约现状。覆盖位置以 conversation_state.task_note_tracking 为准，TaskNoteTracking 没有时间字段，这个值后端没有来源。维持 D8：不进契约，前端不得依赖。前端如需显示“更新于”，属于新的契约需求，要单独提，本次不加。
2. unbound 四个数组恒为空 → 契约现状。GET /note 的 snapshot 来自 build_snapshot，只取 mission_id 已绑定的行；接口没有查询未绑定行的路径。维持 D8：不进契约，前端不得依赖。前端的 snapshot+unbound 合并实际等于只用 snapshot。如需展示未绑定行，属于新的契约需求，本次不加。
3. note_update_end 后“整理中”不消失 → 按原样描述不成立。runner 在 note_update_end 成功时发两次 task_note.updated：第一次在 complete 之前；第二次在 complete 清除 observing 之后，带 note_version（task_note_observer_runner.py 约 429–455 行）。前端防抖按 task_id:agent_instance_id 合并，第二个事件会重置计时器，最后那次拉取一定在 complete 之后，拿到 observing=false。成功路径不改动；另外读代码时发现一个相关缺陷，列为 K1。第 3 条和 K1 都是读代码得出的，没有实际运行验证。

- GET /api/agent-runtime/tasks/{task_id}/agents/{agent_instance_id}/note
  - 200 返回字段：task_id:string，agent_instance_id:string，watermark.covered_until_message_id:string|null，watermark.note_version:int，observing:bool（本实例有观察在运行），snapshot:object（只含 mission_id 已绑定的行），markdown:string。
  - 错误：404，detail 含 AGENT_INSTANCE_NOT_FOUND；503，detail 含 TASK_NOTE_STORAGE_UNAVAILABLE（任何读取失败）。
  - 不进契约，前端不得依赖：mission_id 查询参数与回显；watermark.covered_until_seq（恒为 0）；watermark.updated_at（恒为 null，后端没有来源）；unbound（恒为空数组，接口不提供未绑定行）。
- GET /api/context-compression/observer-prompt（不变）：200 返回 {prompt_text:string}；读正本失败时仍返回 200，prompt_text 以“# 读取提示词失败”开头。
- GET/PUT /api/context-compression/settings（不变）：返回 {enabled, llm_policy_group_id, updated_at}；PUT 请求体为 {llm_policy_group_id}；当前不影响观察者模型（C3）。
- 事件 task_note.updated
  - payload：{task_id, agent_instance_id, scene_id, tool_name, updated_at, note_version?}。
  - 发布流：stream-scene-{scene_id}，没有 scene 时为 stream-agent-task-{task_id}。
  - 语义：该实例的 /note 已过期，需要重新拉取。
  - 补充时序：note_update_end 成功时发布两次，第二次在 observing 清除之后，带 note_version。
  - 补充边界：观察开始、观察失败、被取消、STALE_OBSERVATION 时都不发布（见 K1）。
- 事件 agent.task_note.context_swapped（不变）：payload {task_id, agent_instance_id, conversation_id, note_version, snapshot_message_id}；前端不消费也符合契约。
- 事件 context_compression.*（新增登记）：后端不再发布这组事件。ContextCompressionEventPublisher 没有被实例化，ContextCompressionService.compress_if_needed 也没有调用方。前端对这组事件的投影不会被触发。

## 5. 后端技术架构（含代码架构）与执行清单（后端设计师定稿原文）
档位：轻设计（只补全记忆，不改代码，不新增域；会修改已登记约束 TASK_NOTE-15、TASK_NOTE-18）
代码基准：agentruntime master @ c31db33，加上工作区未提交改动（删除 c31db33 在 task_note_tracking_coordinator.on_round 里加的“首次观察取 max(估算, context_window_used_tokens)”那段，以及对应的测试）。
本次不做：不改代码；冲突 C1~C4 只登记，不修复。

### 5.1 关键取舍
- D1 观察触发口径沿用现有 U1：断点之后的消息估算 ≥ observer_trigger_tokens。不登记“首次观察取真实上下文用量”，以工作区为准。依据：现有记忆 U1；task_note/AGENTS.md 写的是“自上一断点以来的消息增量达到阈值”；真实用量里含系统提示词和工具 Schema，不是观察对象。
- D2 TASK_NOTE-15 补上推进比例和断点消息 ID 两项。依据：AGENTS.md 4 项校验；_validate_note_update_end。
- D3 TASK_NOTE-18 的同步观察条件对根 Agent 和子 Agent 一致。依据：AGENTS.md M6-2；engine 命中门禁后的分支。
- D4 80,000、0.5、0.75 只作为配置默认值写进 designs，不编号。依据：都在 ContextCompressionConfig 里，可配置；用户没有提过数值要求。
- D5 新增不变量：快照标题“# 任务手记快照”是起点丢失时的回退锚点。依据：coordinator、assembler、slicer、engine 四处都靠它回退。
- D6 观察者模型沿用目标 Agent 的执行策略；/settings 的 llm_policy_group_id 当前不生效，列为冲突 C3。
- D7 tracking CAS 规则改成和代码一致：只有 U1 重试一次；U3 失败按观察失败处理；U2 失败靠快照标题回退。依据：Note 工具按主键 upsert 幂等；D5 覆盖 U2 失败后的起点。
- D8 GET /note 只登记有效字段；固定值字段不进契约，前端不得依赖。

### 5.2 代码架构（structure.md 节点标注新增）
- src/agent_runtime/agent/agent_execution_factory.py [task_note·组合根，装配阈值、比例与单例]
- src/agent_runtime/agent/tool_dispatch/tool_dispatcher.py [task_note·业务工具成功后发布 task_note.updated]
- src/agent_runtime/agent/north_gateway/agent_runtime_rest_adapter.py [task_note·GET /note 只读视图]
- src/agent_runtime/context_compression/config.py [task_note·门禁与观察配置]
- src/agent_runtime/context_compression/compression_service.py [task_note·引擎读取配置的入口]
- src/agent_runtime/context_compression/north_gateway/context_compression_rest_adapter.py [task_note·/settings、/observer-prompt]
- src/agent_runtime/context_compression/settings_repository.py [task_note·观察者设置存储]
- src/agent_runtime/task_note/task_note_prompt_provider.py [task_note·system_note_update 正本读取与 Note 工具清单]
- src/agent_runtime/task_note/infrastructure/task_note_schema.py [task_note·启动幂等 DDL 与列断言]
- src/agent_runtime/file_tools/file_edit_tools.py、src/agent_runtime/terminal/terminal_tool.py、src/agent_runtime/subagent/subagent_tool.py、src/agent_runtime/lacp/bridge.py [task_note·事实记录写入点]（TASK_NOTE-02）
域映射 context_compression/** → [task_note] 不变；现有节点标注全部保留，其中 7 个“遗留待清理”继续保留。

### 5.3 技术架构（designs/task_note.md）
双线程观察与前缀替换，新增：
- 方案：触发阈值、推进比例、门禁比例都取 ContextCompressionConfig，默认分别为 80,000、0.5、0.75。
- 方案：两套 token 估算并存。门禁用约 4 字符 1 token；切片和触发用中日韩文字自适应估算（中文约 1.2 token/字，其他约 1 token/3.5 字符）。
- 方案：每次观察新建只在内存里的 conv_obs_* 会话。首条 user 消息包含目标身份（父 ID、祖先链）、目标 Agent 系统提示词、基准快照（去掉 commands）、继承段（子 Agent 且继承段未处理时）、增量 units。LLM 没有发起工具调用时追加提醒后继续；max_worker_rounds 对观察者不生效。
- 方案：观察者模型依次取目标 Agent 执行策略的首个候选、目标 Agent 的 provider/binding、第一个执行策略。
- 方案：替换时先保存会话消息，再执行 U2 写 tracking，两步不在同一事务；尾部首条为 user 时，快照消息沿用它的 ID。
- 不变量：快照消息首个文本块必须以“# 任务手记快照”开头。起点消息 ID 不在当前消息里时，统一回退到最后一条以此开头的消息。
- 不变量：tracking.observing 的 run_id 不在进程内完成事件表时视为重启遗留，在 U1 或替换前等待时清除。
- 保留：现有两条禁止（不得恢复原地切换、不得使用全局锁）和两条不变量（循环无轮数上限、等待不设超时）。
跟踪状态 tracking：
- 改写“CAS 失败重读重算一次，再失败记 error 不推进”为：只有 U1 在 CAS 失败时重读并重试一次；U3 的 CAS 失败按观察失败处理，由调度器收尾清除 observing，这一段之后重新观察；U2 的 CAS 失败不重试，下一轮起点由快照标题回退确定。其余条目保留。
Note 存储：全部保留。
提示词与推送：
- 改写“快照只经 NOTE_SNAPSHOT_JSON 一个通道注入”为：快照 JSON 只经观察会话首条 user 消息注入；系统提示词里的 NOTE_SNAPSHOT_JSON 只渲染为指向该消息的固定说明。
- 改写 task_note.updated 发布方为三处：
  1. 观察者 Note 工具成功后发布；note_update_end 成功时多带 note_version。
  2. 分发器在业务工具（文件增删改移、终端命令、子 Agent 创建与释放、专线消息）或 Note 工具成功后，发给调用方，以及子 Agent、目标 Agent 或被释放的实例。
  3. lacp 入站时发给接收方。
- 改写 /note 条目为第 4 节契约；其余条目保留。

### 5.4 执行步骤
没有代码步骤。

### 5.5 既有约束变更
- TASK_NOTE-15｜修改｜note_update_end 仅限在以下条件同时满足时通过：断点 seq 属于本次 units；断点 seq 及之前的 units 占比不低于 observer_min_advance_ratio；断点 unit 带消息 ID；断点之前的 auto_record 行均已绑定或删除；每个 in_progress Mission 都有 progress。｜依据 D2
- TASK_NOTE-18｜修改｜替换前缀时，若本实例有观察在运行，必须等其结束；note_version 为 0 或断点后增量不低于门禁阈值时，必须先同步完成一次观察再替换；同步观察后 note_version 仍为 0 时不得替换。｜依据 D3
- designs·tracking“CAS 失败重读重算一次…”｜修改｜见 5.3｜依据 D7
- designs·提示词“快照只经 NOTE_SNAPSHOT_JSON 一个通道注入”｜修改｜见 5.3｜依据代码现状
- designs·推送 task_note.updated、/note 两条｜修改｜见 5.3、第 4 节｜依据代码现状、D8
- 架构基线｜新增 2 行｜依据代码现状
  - 持久化：task_note 的 Note 数据用 PostgreSQL（psycopg，复用 cloud_user_config_database），会话 tracking 用 SQLite JSON 加版本列 CAS。
  - 横切：观察会话由后台 asyncio 任务承载，目标身份经 contextvars 注入；task_note 的服务为模块级单例（get_/set_），由 agent_execution_factory 装配。

### 5.6 记忆与源码冲突 / 已知问题（记忆不改，本次不修复）
- C1：TaskNoteStore.get_note_snapshot 仍在读 PG task_note_watermark（get_watermark），违反现有禁止项。
- C2：根 Agent 失败路径（agent_execution_engine 约 4458 行）清理 Note 前没有取消观察，违反现有不变量。
- C3：/settings 的 llm_policy_group_id 没有被 TaskNoteObserverRunner 使用。
- C4：TaskNoteObserverScheduler 给观察者的 delegation_content、fork_message_id 恒为 None。
- K1：observing 状态变化没有事件通知。
  - 观察开始（U1 写入 observing）时不发布事件，前端要等到第一次 Note 工具成功后才知道“整理中”开始了。
  - 观察失败（LLM 调用失败、STALE_OBSERVATION、被取消）时，调度器收尾会清掉 observing，但不发布事件。前端如果此前已拿到 observing=true，“整理中”会一直显示，直到下一个 task_note.updated 到来。
  - 位置：TaskNoteTrackingCoordinator.on_round；TaskNoteObserverScheduler._run_observer_session 的 finally 分支。
  - 依据：读代码推断，没有实际运行验证。
- 工作区未提交改动（task_note_tracking_coordinator.py、tests/test_task_note_tracking_coordinator.py）需要提交，否则 HEAD 的触发口径与 D1 不一致。

已验证：相关 5 个测试文件（tracking_coordinator、observer_trigger、note_context_compression_flow、context_assembler、observer_scheduler）在当前工作区共 26 个用例全部通过。

## 6. 前端技术架构（含代码架构）与执行清单（前端设计师定稿原文）
- 任务：补全 agent-frontend 中 Task Note 展示与更新感知、上下文观察相关的前端架构现状
- 设计档位：轻设计（判据：只解读现状，不新增构件，不引入技术方案）
- 项目形态：老项目，依据 master @ 19d4db6，工作区干净
- 本次不做：不改代码，不写 .arch-memory/，不定义契约，不改造现有偏差

### 6.1 域划分
前端新增域 task_note，域标识与后端 task_note 相同，覆盖以下路径：
- src/features/chat/api/taskNoteApi.ts
- src/features/chat/hooks/useTaskNoteQuery.ts
- src/features/chat/model/taskNote*.ts（taskNoteView.ts、taskNoteArtifacts.ts、taskNoteUiStore.ts）
- src/features/chat/components/TaskNote/**
- src/features/context-compression/**
- scripts/task-note-view.test.mjs

src/features/chat/runtime/chatEventCoordinator.ts 和 src/features/chat/components/TimelinePane.tsx 仍属 chat 公共代码，只在节点标注中写明 task_note 角色：
- chatEventCoordinator：Note 缓存失效
- TimelinePane：Task Note Tab 容器

### 6.2 代码架构现状
| 构件 | 层/类型 | 职责 |
|---|---|---|
| taskNoteApi.ts | api | /note 的 DTO、请求函数 fetchTaskNoteSnapshot、归一化 normalizeTaskNoteResponse（修复 edits 字符串、旧 type 值、缺失数组）、taskNoteKeys、错误分类 classifyTaskNoteError、重试判定 shouldRetryTaskNoteQuery |
| useTaskNoteQuery.ts | hook | Note 快照的唯一读取入口；TaskNotePanel 与 TimelinePane 共用同一份缓存 |
| taskNoteView.ts | model | 视图模型 buildTaskNoteView：snapshot 与 unbound 合并去重、按 Mission 顺序排序、过滤空 progress；另含错误文案 taskNoteErrorView、内容判空 hasTaskNoteContent |
| taskNoteArtifacts.ts | model | foldArtifacts：文件产物按路径折叠为净变化 |
| taskNoteUiStore.ts | store（zustand） | 按实例记录 Inspector Tab；紧凑/展开密度；分节折叠状态 |
| chatEventCoordinator.ts | runtime | SSE 驱动 Note 缓存失效的唯一位置 |
| context-compression/** | 页面 + api | 观察者 LLM 策略的读取与保存（/api/context-compression/settings）；观察者提示词只读展示（/api/context-compression/observer-prompt）；时序图 |

状态归属：
- Note 快照属于服务端状态，放在 TanStack Query，key 为 ["task-note", taskId, agentInstanceId]，staleTime=Infinity
- Tab、密度、折叠属于客户端状态，放在 taskNoteUiStore
- 防抖定时器放在 chatEventCoordinator

### 6.3 前端需求约束（占位编号 → 正式编号见第 9 节）
- TASK_NOTE-01（原 TASK_NOTE-N1）收到 task_note.updated 时，Task Note 快照仅限在用户正查看该 Agent 的 Task Note Tab 时重新请求；其他情况只标记失效，不得发请求，不得显示未读标记。
- TASK_NOTE-02（原 TASK_NOTE-N2）Task Note 快照不得定时轮询，不得在窗口聚焦时刷新。
- TASK_NOTE-03（原 TASK_NOTE-N3）任务结束（agent.task.completed / failed / cancelled，或结束任务会话成功）时必须清除全部 Task Note 缓存。
- TASK_NOTE-04（原 TASK_NOTE-N4）Agent 实例不存在（404）时，Task Note 不得自动重试，不得提供重试入口；认证失效时不得重试。
- TASK_NOTE-05（原 TASK_NOTE-N5）taskId 为空时，Task Note 面板不得发请求。
- TASK_NOTE-06（原 TASK_NOTE-N6）文件类产物必须按路径折叠为净变化展示；同一任务内先创建后删除的文件不得展示。
- TASK_NOTE-07（原 TASK_NOTE-N7）已裁剪的 Mission 仅限展示归档时间与结果摘要。
- TASK_NOTE-08（原 TASK_NOTE-N8）观察者系统提示词在前端仅限只读展示。

以下视觉与布局规则不进架构记忆，属于 UI 设计范畴：截断长度、默认折叠、只显示最近 N 条、W 与 2W 宽度切换。

代码依据（供提炼节点标注用）：
- N1：chatEventCoordinator 954–989 行
- N2：useTaskNoteQuery
- N3：chatEventCoordinator 740 行、ChatPage 1300 行
- N4：taskNoteApi.shouldRetryTaskNoteQuery、taskNoteView.taskNoteErrorView
- N5：TaskNotePanel、useTaskNoteQuery 的 enabled
- N6：taskNoteArtifacts.foldArtifacts
- N7：TaskNoteMissionSection
- N8：contextCompressionApi 只有 GET observer-prompt

### 6.4 架构基线变更
| 原条目 | 动作 | 新内容 | 依据 |
|---|---|---|---|
| 请求统一走 shared/api/httpClient.requestJson，DTO 类型写在 features/chat/api/agentRuntimeApi.ts | 修改 | 请求统一走 shared/api/httpClient.requestJson；DTO、请求函数与 query key 工厂写在 features/<m>/api/<x>Api.ts；读取型 useQuery 封装在 features/<m>/hooks/；视图模型纯函数与 UI zustand store 写在 features/<m>/model/ | 原条目与现状不符，taskNoteApi.ts 等多个 api 文件并存 |
| （新增） | 新增 | SSE 事件驱动的服务端缓存失效只写在 features/chat/runtime/chatEventCoordinator | chatEventCoordinator 740、954–989 行 |

已知偏差：只登记，不改造旧代码，新代码也不沿用。
- context-compression 的 api 文件放在模块根目录，组件内直接写 useQuery，query key 是内联字面量
- Note DTO 类型被 model 与 components 直接 import（如 MissionItem）

### 6.5 转编排的问题（不进约束）
后端（已转后端设计 agent，判定见第 4 节）：
1. GET /tasks/{task_id}/agents/{agent_instance_id}/note 的 watermark.updated_at 固定为 null（agent_runtime_rest_adapter.py 474 行），前端“更新于”永远不显示
2. 同一接口的 unbound 四个数组固定为空（492–497 行），前端的 snapshot+unbound 合并实际不起作用
3. 疑似竞态（读代码推断，没有实际运行验证）：task_note_observer_runner 在 note_update_end 工具成功时，先发布 task_note.updated（351–376 行），再调用 coordinator.complete（379 行起）。前端防抖 150ms 后拉取，可能仍拿到 observing=true；之后没有新的事件，“整理中”可能一直不消失
前端（记录在案，本次不处理）：
4. 前端没有处理 agent.task_note.context_swapped
5. 前端仍在投影旧的 context_compression.* 事件，后端对应的 event_publisher 已是“遗留待清理”
6. ContextCompressionPage 时序图文案与当前机制不一致：仍写“80k Token”和旧工具名 task_note_record_event / add_fact / update_phase，没有 note_update_end 流程

## 8. 本次不做什么
- 不改任何源码与测试；agentruntime 工作区的未提交改动不在本次提交范围
- C1~C4、K1 与前端问题 4~6 只登记，不修复
- 不新增契约字段（watermark.updated_at、未绑定行展示均需另行提出）

## 9. 架构记忆变更
### agentruntime（后端记忆 .arch-memory/）
- business/overview.md：域间关系中调用 task_note 的未登记模块补充 tool 分发器
- business/domains/task_note.md：边界、关键概念、状态流转补充；改写 TASK_NOTE-15、TASK_NOTE-18
- tech/overview.md：架构基线新增“持久化（task_note）”“横切（task_note）”2 行
- tech/designs/task_note.md：双线程观察、tracking、提示词与推送三节按 5.3 与第 4 节更新
- tech/structure.md：节点标注新增 13 个路径（10 条）
### agent-frontend（前端记忆 .arch-memory/）
- business/domains/task_note.md：新建，需求约束 TASK_NOTE-01~08
- business/overview.md：领域清单新增 task_note
- tech/overview.md：架构基线改写 1 行、新增 1 行
- tech/designs/task_note.md：新建，状态归属与读取入口方案、已知偏差禁止项
- tech/structure.md：域映射新增 task_note，节点标注新增
### 编号分配（agent-frontend）
- TASK_NOTE-N1~N8 → TASK_NOTE-01~08（分仓，编号在 agent-frontend 内唯一；与 agentruntime 的 TASK_NOTE-xx 互不相同，跨仓引用写作 agentruntime:TASK_NOTE-xx）
