# 20261014-task-note-observer-trigger

## 0. 元信息
- 仓库：agentruntime master @ 96e708776c8dcf23baa3ba5bffb35f91676ab44e（工作区未提交改动 src/agent_runtime/task_note/task_note_tracking_coordinator.py、tests/test_task_note_tracking_coordinator.py 是本设计前提，见 D1；不随本次提交）
- 设计阶段：Scout 源码召回 → 后端领域咨询 → 后端设计；跳过 UI、前端（界面与前端实现不变）
- 设计档位：后端 标准设计
- 用户确认记录：
  - 后端设计第一版意见，用户原话“观察者一次观察的上限是200轮llm请求。不要加其他的。子 Agent tracking 初始化失败是什么意思啊？子agent创建失败吗？tracking 是什么呀？部署假设 单进程是什么意思啊？系统是可以同时存在多个业务agent并发执行的。”
  - 后端设计第二版（定稿），用户原话“通过”
  - 编排提议整理设计文档、更新后端记忆并顺带改正 M1~M5，用户原话“照做”

## 1. 需求原文
那我就问你要怎么正确切准确的实现这个80k上下文增量触发一次观察！且保证复杂情况下监控靠谱：比如异常，发生历史对话上下文替换（替换的时候是只替换标记最近一次task note了的消息的位置吧）。既然你觉得现在的80k上下文增量的计算是有问题的，那你给出你认为正确的完整的80k上下文增量的统计时序图/流程图啊

前序问题：当前观察者的触发是80ktoken增量触发激活一次观察。这个80k时怎么计算的？你能给出完整的计算过程或是时序图吗？他有使用llm返回的usege信息吗？这个token增量计算他有考虑子agent创建吗？如果业务agent触发了上下文窗口阈值进行了历史对话替换，这个实时增量监控还能正确吗？

## 5. 后端技术架构与执行清单（后端设计师定稿原文）
档位：标准设计（触及事务边界、一致性、容错；改动已登记的不变量和约束）
基准：agentruntime master @ 96e7087（src、tests 与 c31db33 一致），加上工作区未提交改动（定稿 D1）
另行立项，不在本设计范围：R6（重新观察可能写出重复的 Note 行）、K1（observing 变化没有事件）、多进程部署。

### 5.0 术语
tracking = conversation.state.task_note_tracking，每个 Agent 会话一份。内容包括：
- 起点：covered_until（观察者最近一次标记的断点）；prefix（替换后的快照消息，或子 Agent 的边界消息）
- snapshot_message_id
- note_version
- observing（当前运行中的观察）
持久化在 SQLite conversation_state 表的 task_note_tracking 列，task_note_tracking_version 列做版本号；只经 get_task_note_tracking 读、cas_task_note_tracking 按版本号写；save(conversation) 不覆盖它。

### 5.1 替换时替换哪一段（现状说明，新设计不变）
替换只到 covered_until，也就是观察者最近一次标记的断点为止。
例：m1 user、m2 assistant、m3 tool、m4 assistant、m5 user、m6 assistant、m7 tool，covered_until=m4。
- 断点后第一条是 user（m5）：替换后为 [m5′ = 快照文本 + m5 原文，沿用 m5 的 ID]、m6、m7。
- 断点后第一条不是 user：替换后为 [新建快照 user 消息]、尾部原样。
- 替换掉的是 m1~m4，由快照代表。断点之后的全部消息原样保留；系统提示词和工具不动。
- 替换后 tracking：prefix = snapshot = 快照消息，covered 清空。
现状漏洞：计数和再次替换都从 m5′ 的下一条开始，合并在 m5′ 里的 m5 原文被跳过。这导致 K8（少算一条）和 K2（新 usage 回来之前门禁再次命中时，再替换会丢掉 m5）。

### 5.2 80k 增量统计流程（定稿）
核心规则：起算范围 = 起点消息中合并进来的原文（content[1:]）+ 起点之后到当前最后一条消息。计数、观察切片、替换保留的尾部，三处都用这一个规则。

【A. 业务循环检查点】在无工具回复之后、一批工具结果保存之后检查
① 本 Agent 加锁，读 tracking：
   - 本进程内有观察在运行 → 不触发
   - observing 存在，但本进程内没有这个观察（重启遗留）→ 清除 observing，继续
② 确定起点 A：
   - 取 covered_until；没有则取 prefix
   - A 不在当前消息里 → A = 最后一条以“# 任务手记快照”开头的消息
   - 也找不到 → 从第 0 条开始
③ 范围 = A 中合并进来的原文 + A 之后直到当前最后一条
④ 增量 = 范围内每条消息估算值之和：
   - 每轮重新求和，只用一个估算函数
   - 不用 usage；不计系统提示词和工具 Schema
⑤ 增量 ≥ T 时触发：
   - T = min(observer_trigger_tokens, 门禁阈值 × 0.5)
   - observer_trigger_tokens 可从 YAML 配置，默认 80000；配置构造器补读 observer_trigger_tokens 和 observer_min_advance_ratio
   - 触发时用 CAS 写 observing{run_id, 起点 A, 终点 = 当前最后一条}，启动后台观察
   - 终点冻结，之后的新消息留给下一轮

【B. 后台观察】从准备阶段到结束，整体包在一个 try/finally 里
- 流程：切片 → 读快照（失败抛错）→ 观察循环，一次观察最多 200 轮 LLM 请求
- 成功（note_update_end 校验通过）：covered = 断点，note_version + 1，observing 清除
- 其他任何结局都按失败处理：observing 清除，covered 不变，下一个检查点照常重新判断。包括 LLM 失败、200 轮用完、异常、被取消、准备阶段出错、STALE。
- 不加失败冷却，不加时间上限
- 成功写回时 CAS 冲突：重读 tracking，run_id 仍是本次观察就重写，最多 3 次

【C. 门禁】每次真实请求 LLM 之前检查，failover 的每个候选都算
- 预计用量 = usage 基线 + 记录点之后新增消息的估算。预计用量 ≥ 门禁阈值时：
  1. 等待本 Agent 的观察结束。观察最多 200 轮，所以一定会结束；等待本身不设超时
  2. 按 ②③④ 计算断点之后的增量
  3. note_version = 0，或增量 ≥ 门禁阈值 → 同步观察一次
  4. 读快照；读取失败则不替换，本次照常请求
  5. 新消息 = 快照 + ③ 的范围
  6. 保存会话；写 tracking：prefix = snapshot = 快照消息，covered 清空
  7. 立刻重置 usage 基线：基线 = 估算(系统提示词 + 工具 + 新消息)，记录点 = 新消息数
  8. 请求 LLM。之后 failover 时门禁看到的是新基线，不会再次替换

【D. LLM 成功响应】用真实 usage 覆盖基线。响应不带 usage 时，用全量估算作基线。

【E. 子 Agent】
- 创建时写 tracking：prefix = 边界消息，从边界之后开始计数
- 父辈历史只在首次观察时作为背景提供，不计入增量
- tracking 写入失败：重试 1 次；仍失败则本次创建失败，删除已复制的会话，父 Agent 收到工具错误

【部署形态】
- 当前是单个后端进程（server.py 的 uvicorn.run 没有设置 workers）
- 多个业务 Agent 在进程内并发执行，每个 Agent 一把锁，各自计数、各自观察，互不影响
- 多进程共用数据库不在本设计范围内

### 5.3 问题对应处理
| 编号 | 问题 | 新设计 | 与现状比 |
|---|---|---|---|
| K2 | 替换后、新 usage 回来之前再次替换，丢失 user 消息 | 替换后立刻重置 usage 基线；尾部按 ③ 带上合并的原文 | 修改 |
| K3 | 存储故障时用空手记替换历史 | 读快照失败就抛错：替换路径不替换，观察路径按失败处理 | 修改 |
| K4 | 观察准备阶段出错导致 observing 泄漏，业务 Agent 卡住 | 整个观察包在 try/finally 里，一定会收尾 | 修改 |
| K8 | 替换后少算一条 user 消息 | 计数、切片、尾部统一用 ③ | 修改 |
| R1 | 观察者没有上限，业务 Agent 的等待没有期限 | 观察最多 200 轮 LLM 请求，用完按失败处理；业务等待不设超时 | 修改（改不变量） |
| R2 | 80k 不随模型变化，YAML 也改不了 | T = min(配置值, 门禁阈值 × 0.5)；配置构造器补读两个参数 | 修改 |
| R3 | provider 不返回 usage 时门禁永不命中 | 没有 usage 时用全量估算作基线 | 修改 |
| R4 | 估算对图片、PDF 少算 | 维持现状：80k 衡量的是观察者要读的内容，观察者拿到的图片只是引用 | 不变 |
| R5 | 观察失败后每轮都重新触发 | 维持现状：不加冷却 | 不变 |
| R7 | 锁和完成表只在进程内存里 | 维持现状：单进程部署，多 Agent 并发不受影响 | 不变 |
| R8 | 子 Agent tracking 写入失败后从第 0 条开始计数 | 重试 1 次，仍失败则创建失败 | 修改 |
| HEAD 与工作区差异 | 首次观察是否用真实上下文用量 | 按工作区，只用估算（D1）；工作区改动需要提交 | 不变 |

### 5.4 取舍（已随定稿确认）
1. 增量用估算，不用 usage 差值。usage 是整个上下文的量，包括系统提示词、工具和输出；替换后失效；子 Agent 首次的 usage 还包含父辈前缀。
2. 80k 可配置，默认 80000，并且不超过门禁阈值的一半，避免小窗口模型每次都在业务路径上同步观察。
3. 观察者上限为 200 轮 LLM 请求（用户指定），不加其他限制。
4. 子 Agent tracking 写入失败：重试 1 次，仍失败则创建失败。错误的起点会污染子 Agent 的手记，事后无法回收。
5. 部署形态按现状单进程；多进程单独立项。

### 5.5 既有约束变更
| 编号或原条目 | 动作 | 新内容 | 依据 |
|---|---|---|---|
| designs 不变量“观察循环无轮数上限” | 修改 | 一次观察最多 200 轮 LLM 请求，用完按观察失败收尾 | 用户原话“观察者一次观察的上限是200轮llm请求。不要加其他的。” |
| designs 不变量“等待不设超时” | 保留 | 不变；由观察上限保证等待一定会结束 | 同上 |
| designs 方案“触发阈值、推进比例取 ContextCompressionConfig（可配置）” | 修改 | 触发阈值 T = min(observer_trigger_tokens, 门禁阈值 × 0.5)；observer_trigger_tokens 与 observer_min_advance_ratio 由配置构造器从 YAML 读取，默认 80000、0.5 | R2；M1 |
| designs tracking“快照消息只计 content[1:]” | 修改 | 起算范围 = 起点消息中合并的原文 + 起点之后的消息；计数、切片、替换尾部用同一规则 | K2、K8；M2 |
| designs“替换后不刷新 usage”（现状隐含） | 新增 | 替换后立刻重置 usage 基线 = 估算(系统提示词 + 工具 + 新消息)，记录点 = 新消息数；响应不带 usage 时用全量估算作基线 | K2、R3 |
| TASK_NOTE-19“快照读取失败时不得替换上下文” | 保留 | 不变；实现上要求读快照失败时抛错，不得返回空骨架 | K3；M5 |
| designs 不变量“observing 一定由调度器收尾清除” | 新增 | 签发 ticket 之后，从准备阶段到结束都必须有成功或失败收尾 | K4 |
| designs 子 Agent 初始化 | 新增 | init_child 写 tracking 失败时重试 1 次；仍失败则创建子 Agent 失败，删除已复制的会话 | R8 |
| 架构基线 横切 | 新增说明 | 单个后端进程；实例锁和完成表在进程内存，多 Agent 在进程内并发 | R7 |

### 5.6 后续修复建议顺序
K3 → K2/K8 → K4 → R1 → R2/R3 → R8。每项单独立需求，走设计和确认流程。

## 8. 本次不做什么
- 不改源码与测试；工作区 coordinator 改动不随本次提交
- R6、K1、多进程部署另行立项
- 前端不涉及

## 9. 架构记忆变更（agentruntime 后端记忆 .arch-memory/）
- business/domains/task_note.md：新增 TASK_NOTE-20（观察上限 200 轮 LLM 请求，不得设其他上限）；关键概念“观察触发阈值”改为 T 的定义，新增“起算范围”；状态流转补充“200 轮用完”
- tech/designs/task_note.md：按 5.5 改写触发阈值方案、观察上限不变量、起算范围、U3 CAS 重试、子 Agent 初始化；新增 usage 基线重置、快照读取失败不替换、observing 必有收尾、禁止失败冷却与时间上限；M3 删除“Note 工具按主键 upsert 保证幂等”
- tech/overview.md：架构基线“横切（task_note）”补充单进程部署
- tech/structure.md：TASK_NOTE-20 标在 task_note_observer_runner.py
- 编号分配：新增约束直接取号 TASK_NOTE-20（历史最大 19）
- 待澄清：M4（替换与观察路径的快照来自 TaskNoteStore.get_note_snapshot，TASK_NOTE-13 的执行点应标在哪里、该路径是否违反 TASK_NOTE-13 未逐行核实），记忆未改
